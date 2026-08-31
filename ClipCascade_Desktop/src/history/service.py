"""History domain orchestration: bootstrap, capture, query, commands, retention.

Wires crypto.py (DPAPI key + AES-GCM fields) and store.py (SQLite + blobs)
behind the `HistoryService` surface described in the technical plan. Never
imports Qt/PySide6; the history UI process never receives the master key or
direct SQLite access.

Capture wiring into `ClipboardManager` is a later ticket's job. This module
only guarantees the contract `HistorySink`/`HistoryCaptureEvent` exist and
that `HistoryService.record()` works end-to-end, plus the queue-full failure
policy via `QueuedHistorySink`.
"""

import dataclasses
import io
import json
import logging
import os
import queue
import re
import sqlite3
import struct
import threading
from datetime import datetime, timezone
from typing import Optional

from history import crypto, models, retention
from history import store as store_mod

_SUMMARY_TEXT_PREVIEW_CHARS = 500
_FILES_PREVIEW_MAX_NAMES = 3
_RECORDING_ENABLED_KEY = "recording_enabled"
_RETENTION_POLICY_KEY = "retention_policy"

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def classify_text(text: str) -> str:
    """'link' only for a complete http(s) URL with nothing else around it;
    mixed prose containing a URL stays a text entry (per the UI design)."""
    trimmed = text.strip()
    if not trimmed:
        return "text"
    return "link" if _URL_RE.match(trimmed) else "text"


def _pack_files(files: dict) -> bytes:
    """Deterministic single-blob serialization for a file batch."""
    parts = []
    for name in sorted(files.keys()):
        content = files[name]
        name_bytes = name.encode("utf-8")
        parts.append(struct.pack(">I", len(name_bytes)))
        parts.append(name_bytes)
        parts.append(struct.pack(">Q", len(content)))
        parts.append(content)
    return b"".join(parts)


def _unpack_files(blob: bytes) -> dict:
    files = {}
    offset = 0
    length = len(blob)
    while offset < length:
        (name_len,) = struct.unpack_from(">I", blob, offset)
        offset += 4
        name = blob[offset : offset + name_len].decode("utf-8")
        offset += name_len
        (content_len,) = struct.unpack_from(">Q", blob, offset)
        offset += 8
        content = blob[offset : offset + content_len]
        offset += content_len
        files[name] = content
    return files


def _files_preview(names: list) -> str:
    shown = ", ".join(names[:_FILES_PREVIEW_MAX_NAMES])
    remaining = len(names) - _FILES_PREVIEW_MAX_NAMES
    if remaining > 0:
        return f"{shown} and {remaining} more"
    return shown


def _image_dimensions(image_bytes: bytes):
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None, None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.size
    except Exception:
        return None, None


@dataclasses.dataclass
class HistoryInitResult:
    service: Optional["HistoryService"]
    enabled: bool
    quarantined_path: Optional[str] = None
    disabled_reason: Optional[str] = None


def bootstrap(history_dir: str, clock: retention.Clock = None) -> HistoryInitResult:
    """Open (or first-create) the encrypted history store.

    Never raises: any DPAPI/SQLite failure quarantines what it safely can
    and returns a disabled result, so a caller never needs its own
    try/except to keep the rest of the application running. The typed
    failure modes below get a specific `disabled_reason` and (where safe) a
    quarantine; OS-level/SQLite-level storage failures (ACL/pywin32 errors,
    a locked-down/read-only path, a disk-full write, a db header SQLite
    itself cannot read) return a disabled result without quarantining;
    everything else still falls through to the catch-all so it can never
    escape as a raw exception and abort startup.
    """
    clock = clock or retention.SystemClock()
    now = clock.now_utc()

    try:
        try:
            crypto.secure_directory(history_dir)
        except crypto.HistoryUnavailableError as error:
            return HistoryInitResult(None, False, None, f"platform-unsupported: {error}")
        except Exception as error:
            # ACL/pywin32/OSError: nothing safe to quarantine yet, and a
            # broken filesystem must not abort startup.
            return HistoryInitResult(None, False, None, f"storage-unavailable: {error}")

        try:
            key, _created = crypto.load_or_create_master_key(history_dir)
        except crypto.KeyProtectionError as error:
            quarantined_path = store_mod.quarantine_history_directory(history_dir, now)
            return HistoryInitResult(None, False, quarantined_path, f"dpapi-failure: {error}")
        except OSError as error:
            # No quarantine here: moving files needs a working filesystem.
            return HistoryInitResult(None, False, None, f"storage-unavailable: {error}")

        history_store = store_mod.HistoryStore(history_dir)
        try:
            history_store.open()
        except store_mod.SchemaTooNewError as error:
            # Never touched: leave the newer store exactly as found.
            return HistoryInitResult(None, False, None, f"schema-too-new: {error}")
        except store_mod.StoreCorruptError as error:
            quarantined_path = store_mod.quarantine_history_directory(history_dir, now)
            return HistoryInitResult(None, False, quarantined_path, f"store-corrupt: {error}")
        except (sqlite3.Error, OSError) as error:
            # OS/SQLite-level failure with nothing safely quarantinable.
            return HistoryInitResult(None, False, None, f"storage-unavailable: {error}")

        if history_store.get_setting(_RECORDING_ENABLED_KEY) is None:
            history_store.set_setting(_RECORDING_ENABLED_KEY, "1")

        service = HistoryService(history_store, key, clock, history_dir)
        return HistoryInitResult(service, True, None, None)
    except Exception as error:
        return HistoryInitResult(None, False, None, f"unexpected: {error}")


def reset_history_directory(
    history_dir: str, clock: retention.Clock = None, confirm: bool = False
) -> HistoryInitResult:
    """Non-destructive reset: quarantine whatever is on disk, then bootstrap
    a brand-new key and store. Requires explicit confirmation; `bootstrap`
    never calls this on its own."""
    if not confirm:
        raise ValueError("reset_history_directory requires confirm=True")
    clock = clock or retention.SystemClock()
    now = clock.now_utc()
    existing = (
        store_mod.DB_FILE_NAME,
        crypto.MASTER_KEY_FILE_NAME,
        store_mod.BLOBS_DIR_NAME,
    )
    import os

    if os.path.isdir(history_dir) and any(
        os.path.exists(os.path.join(history_dir, name)) for name in existing
    ):
        store_mod.quarantine_history_directory(history_dir, now)
    return bootstrap(history_dir, clock)


class HistoryService:
    def __init__(self, store: store_mod.HistoryStore, key: bytes, clock: retention.Clock, history_dir: str):
        self._store = store
        self._key = key
        self._clock = clock
        self.history_dir = history_dir

    def close(self) -> None:
        self._store.close()

    @property
    def enabled(self) -> bool:
        return self._store.get_setting(_RECORDING_ENABLED_KEY, "1") == "1"

    # --- retention policy persistence -------------------------------------

    def get_retention_policy(self) -> models.RetentionPolicy:
        raw = self._store.get_setting(_RETENTION_POLICY_KEY)
        if not raw:
            return models.RetentionPolicy()
        data = json.loads(raw)
        return models.RetentionPolicy(**data)

    def set_retention_policy(self, policy: models.RetentionPolicy) -> None:
        data = {
            "keep_unpinned_days": policy.keep_unpinned_days,
            "max_unpinned_entries": policy.max_unpinned_entries,
            "max_storage_bytes": policy.max_storage_bytes,
            "keep_pending_transfer_hours": policy.keep_pending_transfer_hours,
        }
        self._store.set_setting(_RETENTION_POLICY_KEY, json.dumps(data))

    # --- crypto helpers ----------------------------------------------------

    def _aad_for_row(self, row: dict) -> bytes:
        return crypto.build_aad(row["id"], row["payload_type"], row["direction"], row["created_at_utc"])

    # --- capture -------------------------------------------------------

    def record(self, event: models.HistoryCaptureEvent) -> Optional[models.HistoryEntryId]:
        if not self.enabled:
            return None

        entry_id = store_mod.new_entry_id()
        created_at = int(event.occurred_at_utc.timestamp())
        aad_of = lambda payload_type: crypto.build_aad(  # noqa: E731
            entry_id, payload_type, event.direction, created_at
        )

        encrypted_payload = None
        blob_relative_path = None
        blob_byte_len = 0
        file_state = None
        expires_at_utc = None

        if event.payload_type == "text":
            text = event.payload
            payload_type = classify_text(text)
            aad = aad_of(payload_type)
            summary = {"preview": text[:_SUMMARY_TEXT_PREVIEW_CHARS]}
            encrypted_payload = crypto.encrypt_field(self._key, text.encode("utf-8"), aad)
            fingerprint_input = _fingerprint_input(payload_type, event.direction, text.encode("utf-8"), event.source_device_id)
        elif event.payload_type == "image":
            payload_type = "image"
            aad = aad_of(payload_type)
            image_bytes = event.payload
            width, height = _image_dimensions(image_bytes)
            summary = {"width": width, "height": height, "byte_size": len(image_bytes)}
            encrypted_blob = crypto.encrypt_field(self._key, image_bytes, aad)
            blob_byte_len = len(encrypted_blob)
            blob_relative_path = self._store.write_blob_atomic(entry_id, encrypted_blob)
            fingerprint_input = _fingerprint_input(payload_type, event.direction, image_bytes, event.source_device_id)
        elif event.payload_type == "files":
            payload_type = "files"
            aad = aad_of(payload_type)
            files = event.payload
            names = sorted(files.keys())
            summary = {
                "preview": _files_preview(names),
                "file_count": len(names),
                "files": [{"name": name, "size_bytes": len(files[name])} for name in names],
            }
            packed = _pack_files(files)
            encrypted_blob = crypto.encrypt_field(self._key, packed, aad)
            blob_byte_len = len(encrypted_blob)
            blob_relative_path = self._store.write_blob_atomic(entry_id, encrypted_blob)
            fingerprint_input = _fingerprint_input(payload_type, event.direction, packed, event.source_device_id)
            file_state = "ready"
            hours = self.get_retention_policy().keep_pending_transfer_hours
            if hours is not None:
                expires_at_utc = created_at + hours * 3600
        else:
            raise ValueError(f"Unsupported capture payload_type: {event.payload_type!r}")

        summary_bytes = json.dumps(summary, separators=(",", ":")).encode("utf-8")
        encrypted_summary = crypto.encrypt_field(self._key, summary_bytes, aad)

        byte_size = len(encrypted_summary)
        byte_size += len(encrypted_payload) if encrypted_payload else 0
        byte_size += blob_byte_len

        source_device_id_hash = None
        encrypted_source_device = None
        if event.source_device_id:
            source_device_id_hash = crypto.fingerprint(event.source_device_id.encode("utf-8"))
        if event.source_device_name:
            encrypted_source_device = crypto.encrypt_field(
                self._key, event.source_device_name.encode("utf-8"), aad
            )

        row = {
            "id": entry_id,
            "created_at_utc": created_at,
            "updated_at_utc": created_at,
            "payload_type": payload_type,
            "direction": event.direction,
            "transport": event.transport,
            "pinned": 0,
            "byte_size": byte_size,
            "encrypted_summary": encrypted_summary,
            "encrypted_payload": encrypted_payload,
            "blob_relative_path": blob_relative_path,
            "payload_sha256": crypto.fingerprint(fingerprint_input),
            "source_device_id_hash": source_device_id_hash,
            "encrypted_source_device": encrypted_source_device,
            "file_state": file_state,
            "expires_at_utc": expires_at_utc,
            "downloaded_directory_encrypted": None,
        }
        self._store.insert_entry(row)
        return entry_id

    # --- query/detail ----------------------------------------------------

    def _to_summary(self, row: dict) -> models.HistoryEntrySummary:
        aad = self._aad_for_row(row)
        summary = json.loads(crypto.decrypt_field(self._key, row["encrypted_summary"], aad).decode("utf-8"))
        source_device_name = None
        if row["encrypted_source_device"]:
            source_device_name = crypto.decrypt_field(
                self._key, row["encrypted_source_device"], aad
            ).decode("utf-8")
        return models.HistoryEntrySummary(
            id=row["id"],
            created_at_utc=datetime.fromtimestamp(row["created_at_utc"], tz=timezone.utc),
            updated_at_utc=datetime.fromtimestamp(row["updated_at_utc"], tz=timezone.utc),
            payload_type=row["payload_type"],
            direction=row["direction"],
            transport=row["transport"],
            pinned=bool(row["pinned"]),
            byte_size=row["byte_size"],
            preview=summary.get("preview", ""),
            source_device_name=source_device_name,
            file_state=row["file_state"],
            file_count=summary.get("file_count"),
            image_width=summary.get("width"),
            image_height=summary.get("height"),
        )

    def query(
        self, filter: models.HistoryFilter = None, cursor: str = None, page_size: int = 50
    ) -> models.HistoryPage:
        filter = filter or models.HistoryFilter()
        rows, has_more = self._store.list_entries(
            payload_types=tuple(filter.payload_types) if filter.payload_types else None,
            direction=filter.direction,
            pinned_only=filter.pinned_only,
            cursor=cursor,
            limit=page_size,
        )
        entries = tuple(self._to_summary(row) for row in rows)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = store_mod.encode_cursor(last["created_at_utc"], last["id"])
        return models.HistoryPage(entries=entries, next_cursor=next_cursor, has_more=has_more)

    def get_detail(self, entry_id: models.HistoryEntryId) -> Optional[models.HistoryEntryDetail]:
        row = self._store.get_entry(entry_id)
        if row is None:
            return None
        summary = self._to_summary(row)
        aad = self._aad_for_row(row)
        summary_json = json.loads(crypto.decrypt_field(self._key, row["encrypted_summary"], aad).decode("utf-8"))

        text = None
        url = None
        image_bytes = None
        files = ()
        downloaded_directory = None

        if row["payload_type"] in ("text", "link") and row["encrypted_payload"]:
            decrypted = crypto.decrypt_field(self._key, row["encrypted_payload"], aad).decode("utf-8")
            if row["payload_type"] == "link":
                url = decrypted
            else:
                text = decrypted
        elif row["payload_type"] == "image" and row["blob_relative_path"]:
            encrypted_blob = self._store.read_blob(row["blob_relative_path"])
            image_bytes = crypto.decrypt_field(self._key, encrypted_blob, aad)
        elif row["payload_type"] == "files":
            files = tuple(
                models.FileBatchItem(name=item["name"], size_bytes=item["size_bytes"])
                for item in summary_json.get("files", [])
            )

        if row["downloaded_directory_encrypted"]:
            downloaded_directory = crypto.decrypt_field(
                self._key, row["downloaded_directory_encrypted"], aad
            ).decode("utf-8")

        summary_fields = {f.name: getattr(summary, f.name) for f in dataclasses.fields(summary)}
        return models.HistoryEntryDetail(
            **summary_fields,
            text=text,
            url=url,
            image_bytes=image_bytes,
            files=files,
            downloaded_directory=downloaded_directory,
        )

    def get_file_batch_payload(self, entry_id: models.HistoryEntryId) -> dict:
        """Decrypted {filename: bytes} for a 'files' entry while its transfer
        bytes are still `ready`. Raises if the type or state doesn't match —
        callers (a later download command) are expected to check state first."""
        row = self._store.get_entry(entry_id)
        if row is None:
            raise KeyError(entry_id)
        if row["payload_type"] != "files":
            raise ValueError(f"{entry_id} is not a files entry")
        if not row["blob_relative_path"]:
            raise ValueError(f"{entry_id} has no retrievable transfer bytes (state={row['file_state']!r})")
        aad = self._aad_for_row(row)
        encrypted_blob = self._store.read_blob(row["blob_relative_path"])
        packed = crypto.decrypt_field(self._key, encrypted_blob, aad)
        return _unpack_files(packed)

    # --- commands --------------------------------------------------------

    def execute(self, command: models.Command) -> models.CommandResult:
        now = self._clock.now_utc()

        if isinstance(command, models.PinEntryCommand):
            ok = self._store.set_pinned(command.entry_id, True, now)
            return models.CommandResult(ok=ok, entry_id=command.entry_id, error=None if ok else "not-found")

        if isinstance(command, models.UnpinEntryCommand):
            ok = self._store.set_pinned(command.entry_id, False, now)
            return models.CommandResult(ok=ok, entry_id=command.entry_id, error=None if ok else "not-found")

        if isinstance(command, models.DeleteEntryCommand):
            result = self._delete_entries([command.entry_id])
            if result.affected_count == 0:
                return models.CommandResult(ok=False, entry_id=command.entry_id, error="not-found")
            return models.CommandResult(ok=True, entry_id=command.entry_id, affected_count=1)

        if isinstance(command, models.ClearUnpinnedCommand):
            ids = [row["id"] for row in self._store.list_unpinned_oldest_first()]
            return self._delete_entries(ids)

        if isinstance(command, models.ClearAllCommand):
            return self._delete_entries(self._store.list_all_ids())

        if isinstance(command, models.ExpireTransfersNowCommand):
            policy = models.RetentionPolicy(
                keep_unpinned_days=None,
                max_unpinned_entries=None,
                max_storage_bytes=None,
                keep_pending_transfer_hours=self.get_retention_policy().keep_pending_transfer_hours,
            )
            result = retention.apply(self._store, policy, self._clock)
            return models.CommandResult(ok=True, affected_count=result.transfer_entries_expired)

        if isinstance(command, models.SetRecordingEnabledCommand):
            self._store.set_setting(_RECORDING_ENABLED_KEY, "1" if command.enabled else "0")
            return models.CommandResult(ok=True)

        if isinstance(command, models.MarkEntryDownloadedCommand):
            return self._mark_entry_downloaded(command)

        return models.CommandResult(ok=False, error=f"Unsupported command: {command!r}")

    def _mark_entry_downloaded(self, command: models.MarkEntryDownloadedCommand) -> models.CommandResult:
        now = self._clock.now_utc()
        row = self._store.get_entry(command.entry_id)
        if row is None:
            return models.CommandResult(ok=False, entry_id=command.entry_id, error="not-found")
        if row["payload_type"] != "files":
            return models.CommandResult(ok=False, entry_id=command.entry_id, error="not-a-file-batch")
        if row["file_state"] != "ready":
            return models.CommandResult(
                ok=False, entry_id=command.entry_id, error=f"invalid-state:{row['file_state']}"
            )
        directory = os.path.normpath(command.downloaded_directory)
        if not os.path.isdir(directory):
            return models.CommandResult(ok=False, entry_id=command.entry_id, error="directory-missing")
        aad = self._aad_for_row(row)
        encrypted_dir = crypto.encrypt_field(self._key, directory.encode("utf-8"), aad)
        self._store.set_file_state(
            command.entry_id,
            "downloaded",
            now,
            downloaded_directory_encrypted=encrypted_dir,
        )
        return models.CommandResult(ok=True, entry_id=command.entry_id, affected_count=1)

    def _delete_entries(self, entry_ids) -> models.CommandResult:
        if not entry_ids:
            return models.CommandResult(ok=True, affected_count=0)
        deleted_rows = self._store.delete_entries(entry_ids)
        for row in deleted_rows:
            blob_path = row.get("blob_relative_path")
            if blob_path:
                self._store.delete_blob(blob_path)  # best-effort; orphan_cleanup retries on failure
        return models.CommandResult(ok=True, affected_count=len(deleted_rows))

    # --- retention ---------------------------------------------------------

    def preview_retention(self, policy: models.RetentionPolicy = None) -> models.RetentionImpact:
        policy = policy or self.get_retention_policy()
        return retention.preview_impact(self._store, policy, self._clock)

    def apply_retention(self, policy: models.RetentionPolicy = None) -> models.RetentionResult:
        policy = policy or self.get_retention_policy()
        result = retention.apply(self._store, policy, self._clock)
        self.set_retention_policy(policy)
        return result

    def orphan_cleanup(self, grace_period_seconds: int = 3600) -> dict:
        return self._store.orphan_cleanup(grace_period_seconds)


def _fingerprint_input(payload_type: str, direction: str, canonical_bytes: bytes, source_device_id: Optional[str]) -> bytes:
    parts = [
        payload_type.encode("utf-8"),
        b"|",
        direction.encode("utf-8"),
        b"|",
        (source_device_id or "").encode("utf-8"),
        b"|",
        canonical_bytes,
    ]
    return b"".join(parts)


class QueuedHistorySink:
    """Bounded-queue `HistorySink` wrapping a `HistoryService`.

    `record()` never blocks the caller (clipboard capture must never wait on
    SQLite/DPAPI): it enqueues and returns immediately. When the queue is
    full, the event is dropped and only a metadata-only warning is logged —
    matches the "history write queue full" failure policy. A background
    thread performs the actual (synchronous) `HistoryService.record()`.
    """

    def __init__(self, service: HistoryService, maxsize: int = 200, on_recorded=None):
        self._service = service
        self._queue = queue.Queue(maxsize=maxsize)
        self.dropped_count = 0
        self._stop = threading.Event()
        self._on_recorded = on_recorded
        self._worker = threading.Thread(
            target=self._run, name="clipcascade-history-writer", daemon=True
        )
        self._worker.start()

    def record(self, event: models.HistoryCaptureEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped_count += 1
            logging.warning(
                "History write queue full; dropped a %s/%s capture event "
                "(metadata only, no payload logged)",
                event.payload_type,
                event.direction,
            )

    def _run(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                entry_id = self._service.record(event)
                if entry_id is not None and self._on_recorded is not None:
                    try:
                        self._on_recorded(entry_id)
                    except Exception:
                        logging.exception(
                            "History IPC notification failed after a successful write"
                        )
            except Exception:
                logging.exception("History write failed; capture event dropped (metadata only)")
            finally:
                self._queue.task_done()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._worker.join(timeout)
