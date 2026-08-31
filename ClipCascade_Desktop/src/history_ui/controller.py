"""Read-only data orchestration plus action dispatch for the history window.

`ReadOnlyHistoryGateway` is the *only* way the UI touches the IPC channel.
Its allow-list below is the complete mutation surface: history commands
(pin/unpin/delete/clear/mark-downloaded), retention preview/apply and the
main-process action endpoints (copy again, downloads, folder open, clipboard
clear) that T6 routes through the authenticated pipe. Raw commands can only
be expressed through `execute_command`, whose accepted names the IPC server
validates server-side. (`history_ui.client.HistoryIpcClient` carries the raw
plumbing from T4; nothing outside this facade may reach it, and tests assert
the non-controller UI sources never do.)

`HistoryController` owns paging, in-memory search (150 ms debounce over the
decrypted bounded summaries the server already returned -- there is no
persistent plaintext index), ordering, live IPC events, announcements and
action dispatch on a QThreadPool (the Qt event loop -- painting and keyboard
handling -- never blocks on the pipe). Tests can flip the controller into
synchronous mode for deterministic assertions.
"""

import re

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from history import ipc_protocol

from history_ui import theme

PAGE_SIZE = 100
MAX_LOADED_ENTRIES = ipc_protocol.MAX_PAGE_SIZE
SEARCH_DEBOUNCE_MS = 150

ALLOWED_ACTIONS = (
    "ping",
    "query",
    "get_detail",
    "execute_command",
    "preview_retention",
    "apply_retention",
    "copy_again",
    "download_files",
    "save_image",
    "open_folder",
    "copy_file_paths",
    "clear_windows_clipboard",
)

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class ReadOnlyHistoryGateway:
    """Allow-listed facade over the T4 IPC client."""

    def __init__(self, client):
        self._client = client

    @property
    def client(self):
        return self._client

    def ping(self):
        return self._client.ping()

    def query(self, payload_types=None, direction=None, pinned_only=False, cursor=None, page_size=PAGE_SIZE):
        return self._client.query(
            payload_types=payload_types,
            direction=direction,
            pinned_only=pinned_only,
            cursor=cursor,
            page_size=page_size,
        )

    def get_detail(self, entry_id):
        return self._client.get_detail(entry_id)

    def execute_command(self, command, **fields):
        return self._client.execute_command(command, **fields)

    def preview_retention(self, policy=None):
        return self._client.preview_retention(policy)

    def apply_retention(self, policy=None):
        return self._client.apply_retention(policy)

    def copy_again(self, entry_id):
        return self._client.copy_again(entry_id)

    def download_files(self, entry_id, target_directory, filenames=None):
        return self._client.download_files(entry_id, target_directory, filenames)

    def save_image(self, entry_id, target_path):
        return self._client.save_image(entry_id, target_path)

    def open_folder(self, entry_id):
        return self._client.open_folder(entry_id)

    def copy_file_paths(self, entry_id):
        return self._client.copy_file_paths(entry_id)

    def clear_windows_clipboard(self):
        return self._client.clear_windows_clipboard()


class _TaskSignalEmitter(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)


class _Worker(QRunnable):
    def __init__(self, emitter, task_id, fn):
        super().__init__()
        self._emitter = emitter
        self._task_id = task_id
        self._fn = fn

    def run(self):
        try:
            self._emitter.done.emit(self._task_id, self._fn())
        except Exception as error:  # noqa: BLE001 - surfaced to the UI as a state
            self._emitter.failed.emit(self._task_id, f"{type(error).__name__}: {error}")


def extract_hostname(text):
    """First http(s) hostname inside a summary, for search over 'hostname'."""
    match = _URL_IN_TEXT_RE.search(text or "")
    if match is None:
        return ""
    try:
        from urllib.parse import urlsplit

        return urlsplit(match.group(0)).hostname or ""
    except ValueError:
        return ""


class HistoryController(QObject):
    rows_changed = Signal(list)
    state_changed = Signal(str)
    status_changed = Signal(str)
    detail_loading = Signal(str)
    detail_loaded = Signal(object)
    detail_failed = Signal(str, str)
    announced = Signal(str)
    action_completed = Signal(str, object)
    action_failed = Signal(str, str)

    def __init__(self, gateway, parent=None, synchronous=False, search_debounce_ms=SEARCH_DEBOUNCE_MS):
        super().__init__(parent)
        self._gateway = gateway
        self._pool = QThreadPool.globalInstance()
        self._synchronous = synchronous
        self._emitter = _TaskSignalEmitter()
        self._emitter.done.connect(self._on_task_done)
        self._emitter.failed.connect(self._on_task_failed)

        self._all_loaded = []
        self._next_cursor = None
        self._has_more = False
        self._loading_more = False
        self._generation = 0
        self._type_filter = "all"
        self._source_filter = "all"
        self._search_text = ""
        self._oldest_first = False
        self._selected_id = None
        self._pending_detail_id = None
        self._last_announcement = ""
        self._failure = None
        self._initial_loaded = False

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(search_debounce_ms)
        self._search_timer.timeout.connect(self._apply_search)

    # --- public read-only surface -------------------------------------------

    @property
    def entries_loaded(self):
        return len(self._all_loaded)

    @property
    def selected_id(self):
        return self._selected_id

    @property
    def last_announcement(self):
        return self._last_announcement

    @property
    def failure(self):
        return self._failure

    def start(self):
        """First load: paint the newest page fast, then keep filling the
        bounded window (up to MAX_LOADED_ENTRIES) in the background."""
        self._generation += 1
        self._initial_loaded = False
        self._failure = None
        self._set_state("loading")
        self._enqueue_query(cursor=None, gen=self._generation, fill=True)

    def set_type_filter(self, type_filter):
        if type_filter not in ("all", "pinned", "text", "link", "image", "files"):
            return
        self._type_filter = type_filter
        self._rebuild()

    def set_source_filter(self, source_filter):
        if source_filter not in ("all", "local", "remote"):
            return
        self._source_filter = source_filter
        self._rebuild()

    def set_order(self, oldest_first):
        self._oldest_first = bool(oldest_first)
        self._rebuild()

    def set_search_text(self, text):
        """Debounced: typing restarts the timer; the actual filter lands
        SEARCH_DEBOUNCE_MS after the last keystroke, in memory only."""
        self._search_text = text or ""
        self._search_timer.start()

    def search_text(self):
        return self._search_text

    def request_more(self):
        """View-driven paging: fetch the next summary page with the server
        cursor. Summaries only -- full image/file payloads are fetched once,
        for the single selected entry, via get_detail."""
        if self._loading_more or not self._has_more or not self._next_cursor:
            return
        if len(self._all_loaded) >= MAX_LOADED_ENTRIES:
            self._has_more = False
            return
        self._loading_more = True
        self._enqueue_query(cursor=self._next_cursor, gen=self._generation, fill=True)

    def select_entry(self, entry_id):
        if entry_id is None or entry_id == self._selected_id:
            return
        self._selected_id = entry_id
        self._pending_detail_id = entry_id
        self.detail_loading.emit(entry_id)
        self._enqueue_detail(entry_id, self._generation)

    def execute_action(self, name, args=None):
        """Run a gateway action off the event loop. Results arrive via
        action_completed/action_failed; state-changing successes also arrive
        as IPC entry/retention events through handle_event."""
        if name not in ALLOWED_ACTIONS or name in ("ping", "query", "get_detail"):
            self.action_failed.emit(name, f"unsupported-action:{name}")
            return

        gateway = self._gateway

        def job():
            method = getattr(gateway, name)
            return method(**(args or {}))

        self._submit(f"action:{name}", self._generation, job)

    def handle_event(self, name, data, gap=False):
        """IPC events. Deletions are applied in place; additions and updates
        trigger a bounded re-query of the loaded window (summaries only, so
        no image/file payload ever rides a refresh)."""
        if name == "entry_deleted":
            entry_id = (data or {}).get("entry_id")
            self._all_loaded = [row for row in self._all_loaded if row.get("id") != entry_id]
            if self._selected_id == entry_id:
                self._selected_id = None
                self.detail_failed.emit(entry_id or "", "Entry no longer available")
            self._rebuild()
            self._announce(f"History entry removed. {self._status_text()}")
            return
        if name in ("entry_added", "entry_updated", "retention_changed", "service_disabled") or gap:
            self.refresh_loaded(announce=name == "entry_added")

    def refresh_loaded(self, announce=False):
        """Re-fetch the currently loaded window of summaries (bounded)."""
        self._generation += 1
        target = len(self._all_loaded)
        self._enqueue_refresh(target_pages=max(1, -(-target // PAGE_SIZE)), gen=self._generation, announce=announce)

    # --- filter/search matching ----------------------------------------------

    def _matches_type(self, row):
        if self._type_filter == "all":
            return True
        if self._type_filter == "pinned":
            return bool(row.get("pinned"))
        return row.get("payload_type") == self._type_filter

    def _matches_source(self, row):
        if self._source_filter == "all":
            return True
        return row.get("direction") == self._source_filter

    def _matches_search(self, row):
        needle = self._search_text.strip().lower()
        if not needle:
            return True
        haystacks = [
            row.get("preview", ""),
            row.get("source_device_name") or "",
            row.get("payload_type", ""),
            theme.TYPE_LABELS.get(row.get("payload_type", ""), ""),
            extract_hostname(row.get("preview", "")),
        ]
        return any(needle in (hay or "").lower() for hay in haystacks)

    # --- internals ------------------------------------------------------------

    def _server_query_kwargs(self):
        payload_types = None
        if self._type_filter in ("text", "link", "image", "files"):
            payload_types = [self._type_filter]
        direction = None if self._source_filter == "all" else self._source_filter
        return {
            "payload_types": payload_types,
            "direction": direction,
            "pinned_only": self._type_filter == "pinned",
        }

    def _enqueue_query(self, cursor, gen, fill):
        kwargs = self._server_query_kwargs()

        def job():
            return self._gateway.query(cursor=cursor, page_size=PAGE_SIZE, **kwargs)

        self._submit("query", gen, job, append=cursor is not None)

    def _enqueue_refresh(self, target_pages, gen, announce):
        kwargs = self._server_query_kwargs()
        cursors = [None]

        def job():
            rows = []
            cursor = None
            has_more = False
            next_cursor = None
            for _ in range(target_pages):
                page = self._gateway.query(cursor=cursor, page_size=PAGE_SIZE, **kwargs)
                rows.extend(page.get("entries", []))
                has_more = bool(page.get("has_more"))
                next_cursor = page.get("next_cursor")
                cursor = next_cursor
                if not has_more or not cursor:
                    break
            return {"entries": rows, "has_more": has_more, "next_cursor": next_cursor, "cursors": cursors}

        self._submit("refresh", gen, job, announce=announce)

    def _enqueue_detail(self, entry_id, gen):
        def job():
            return self._gateway.get_detail(entry_id)

        self._submit("detail", gen, job, entry_id=entry_id)

    def _submit(self, task_id, gen, job, **payload):
        emitter = _TaskSignalEmitter()
        emitter.done.connect(self._on_task_done)
        emitter.failed.connect(self._on_task_failed)
        if self._synchronous:
            try:
                emitter.done.emit(task_id, {"gen": gen, **payload, "result": job()})
            except Exception as error:  # noqa: BLE001
                emitter.failed.emit(task_id, f"{type(error).__name__}: {error}")
        else:
            worker = _Worker(
                emitter,
                task_id,
                lambda: {"gen": gen, **payload, "result": job()},
            )
            self._pool.start(worker)

    def _on_task_done(self, task_id, boxed):
        gen = boxed.get("gen")
        if gen is not None and gen != self._generation and task_id in ("query", "refresh"):
            return
        result = boxed.get("result")
        if task_id == "query":
            self._on_page(result, append=boxed.get("append", False))
        elif task_id == "refresh":
            self._on_refresh(result, announce=boxed.get("announce", False))
        elif task_id == "detail":
            self._on_detail(boxed.get("entry_id"), result)
        elif task_id.startswith("action:"):
            self.action_completed.emit(task_id.split(":", 1)[1], result)

    def _on_task_failed(self, task_id, message):
        if task_id in ("query", "refresh"):
            if not self._initial_loaded:
                self._failure = message
                self._set_state("error")
            else:
                self._has_more = False
        elif task_id == "detail":
            entry_id = self._pending_detail_id or ""
            self._pending_detail_id = None
            self.detail_failed.emit(entry_id, message)
        elif task_id.startswith("action:"):
            self.action_failed.emit(task_id.split(":", 1)[1], message)

    def _on_page(self, page, append):
        if not isinstance(page, dict):
            self._set_state("error")
            return
        entries = page.get("entries", [])
        self._next_cursor = page.get("next_cursor")
        self._has_more = bool(page.get("has_more"))
        self._loading_more = False
        if append:
            self._all_loaded.extend(entries)
        else:
            self._all_loaded = list(entries)
        if not self._initial_loaded:
            self._initial_loaded = True
            self._set_state("ready")
            self._announce(self._status_text())
        self._rebuild()
        if self._has_more and self._next_cursor and len(self._all_loaded) < MAX_LOADED_ENTRIES:
            self._enqueue_query(cursor=self._next_cursor, gen=self._generation, fill=True)

    def _on_refresh(self, page, announce):
        if not isinstance(page, dict):
            return
        self._all_loaded = list(page.get("entries", []))
        self._next_cursor = page.get("next_cursor")
        self._has_more = bool(page.get("has_more"))
        self._rebuild()
        if announce:
            self._announce(f"History updated. {self._status_text()}")

    def _on_detail(self, entry_id, detail):
        self._pending_detail_id = None
        if entry_id != self._selected_id:
            return
        if not isinstance(detail, dict):
            self.detail_failed.emit(entry_id or "", "Detail unavailable")
            return
        self.detail_loaded.emit(detail)

    def _apply_search(self):
        self._rebuild()

    def _visible_rows(self):
        rows = [row for row in self._all_loaded if self._matches_type(row) and self._matches_source(row)]
        if self._search_text.strip():
            rows = [row for row in rows if self._matches_search(row)]
        if self._oldest_first:
            rows = list(reversed(rows))
        return rows

    def _rebuild(self):
        rows = self._visible_rows()
        self.rows_changed.emit(rows)
        if not self._all_loaded:
            self._set_state("loading" if not self._initial_loaded else "empty")
        elif not rows:
            self._set_state("search_empty" if self._search_text.strip() else "empty")
        else:
            self._set_state("ready")
        self.status_changed.emit(self._status_text())

    def _set_state(self, state):
        self.state_changed.emit(state)

    def _status_text(self):
        shown = len(self._visible_rows())
        total = len(self._all_loaded)
        pinned = sum(1 for row in self._all_loaded if row.get("pinned"))
        if not self._initial_loaded:
            return "Loading history…"
        pieces = []
        if shown != total:
            pieces.append(f"{shown} of {total} entries")
        else:
            pieces.append(f"{total} entries")
        if pinned:
            pieces.append(f"{pinned} pinned")
        if self._has_more:
            pieces.append("loading more…")
        return " · ".join(pieces)

    def _announce(self, text):
        self._last_announcement = text
        self.announced.emit(text)

    def type_counts(self):
        counts = {
            "all": len(self._all_loaded),
            "pinned": 0,
            "text": 0,
            "link": 0,
            "image": 0,
            "files": 0,
            "local": 0,
            "remote": 0,
        }
        for row in self._all_loaded:
            counts["pinned"] += 1 if row.get("pinned") else 0
            counts["local"] += 1 if row.get("direction") == "local" else 0
            counts["remote"] += 1 if row.get("direction") == "remote" else 0
            payload_type = row.get("payload_type")
            if payload_type in counts:
                counts[payload_type] += 1
        return counts
