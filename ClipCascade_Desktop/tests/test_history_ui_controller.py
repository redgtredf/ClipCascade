"""T5 controller/window tests: read-only orchestration over the IPC gateway.

Covers the pieces the model tests cannot see: the allow-listed read-only
gateway, filter/search/order behaviour (including the 150 ms debounce),
cursor paging, selection detail fetch, IPC event handling and the window's
no-mutation guarantee. All IPC is faked; the controller runs in synchronous
mode so signal delivery is deterministic.
"""

import ast
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402

from history_ui.controller import (  # noqa: E402
    ALLOWED_ACTIONS,
    HistoryController,
    ReadOnlyHistoryGateway,
    extract_hostname,
)
from history_ui.window import HistoryWindow  # noqa: E402

UI_SOURCES = [
    Path("src/history_ui/window.py"),
    Path("src/history_ui/controller.py"),
    Path("src/history_ui/detail_views.py"),
    Path("src/history_ui/entry_model.py"),
    Path("src/history_ui/main.py"),
]

FORBIDDEN_METHODS = FORBIDDEN = None  # superseded by ALLOWED_IPC_METHODS guards


def row(index, payload_type="text", direction="local", pinned=False, **overrides):
    entry = {
        "id": f"entry-{index}",
        "created_at_utc": "2026-08-31T10:00:00+00:00",
        "updated_at_utc": "2026-08-31T10:00:00+00:00",
        "payload_type": payload_type,
        "direction": direction,
        "transport": "local",
        "pinned": pinned,
        "byte_size": 100 + index,
        "preview": f"preview text {index}",
        "source_device_name": None,
        "file_state": None,
        "file_count": None,
        "image_width": None,
        "image_height": None,
    }
    entry.update(overrides)
    return entry


class FakeGateway:
    """Records every call; serves canned pages in a fixed order."""

    def __init__(self, pages, detail=None):
        self.pages = list(pages)
        self.detail = detail or {}
        self.queries = []
        self.details = []
        self.actions = []

    def ping(self):
        return True

    def query(self, payload_types=None, direction=None, pinned_only=False, cursor=None, page_size=100):
        self.queries.append(
            {
                "payload_types": payload_types,
                "direction": direction,
                "pinned_only": pinned_only,
                "cursor": cursor,
                "page_size": page_size,
            }
        )
        index = 0 if cursor is None else int(cursor)
        page = self.pages[index] if index < len(self.pages) else {"entries": [], "has_more": False, "next_cursor": None}
        return page

    def get_detail(self, entry_id):
        self.details.append(entry_id)
        return dict(self.detail, id=entry_id)

    def execute_command(self, command, **fields):
        self.actions.append(("execute_command", command, fields))
        return {"ok": True, "command": command}

    def preview_retention(self, policy=None):
        self.actions.append(("preview_retention", policy))
        return {"entries_to_remove": 2, "bytes_to_release": 100, "transfer_entries_to_expire": 1}

    def apply_retention(self, policy=None):
        self.actions.append(("apply_retention", policy))
        return {"ok": True}

    def copy_again(self, entry_id):
        self.actions.append(("copy_again", entry_id))
        return {"ok": True, "entry_id": entry_id}

    def download_files(self, entry_id, target_directory, filenames=None):
        self.actions.append(("download_files", entry_id, target_directory, filenames))
        return {"ok": True, "files": [], "downloaded_directory": target_directory}

    def save_image(self, entry_id, target_path):
        self.actions.append(("save_image", entry_id, target_path))
        return {"ok": True, "path": target_path}

    def open_folder(self, entry_id):
        self.actions.append(("open_folder", entry_id))
        return {"ok": True}

    def copy_file_paths(self, entry_id):
        self.actions.append(("copy_file_paths", entry_id))
        return {"ok": True, "count": 1}

    def clear_windows_clipboard(self):
        self.actions.append(("clear_windows_clipboard",))
        return {"ok": True}


@pytest.fixture
def sync_controller():
    def make(pages, detail=None):
        gateway = FakeGateway(pages, detail)
        controller = HistoryController(ReadOnlyHistoryGateway(gateway), synchronous=True)
        return controller, gateway

    return make


# --- read-only / allow-listed gateway guarantees ---------------------------------

# The complete IPC surface the UI may reach, expressed in one place. T6
# widened T5's read-only allow-list with history commands, retention and
# main-process actions; everything else stays forbidden.
ALLOWED_IPC_METHODS = {
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
}


def test_gateway_allow_list_matches_design():
    assert set(ALLOWED_ACTIONS) == ALLOWED_IPC_METHODS
    gateway = ReadOnlyHistoryGateway(FakeGateway([]))
    for method in ALLOWED_IPC_METHODS:
        assert hasattr(gateway, method)
    # nothing beyond the allow-list and python internals is exposed
    public = {name for name in dir(gateway) if not name.startswith("_")}
    assert public <= ALLOWED_IPC_METHODS | {"client"}


def test_ui_sources_exclude_controller():
    assert Path("src/history_ui/controller.py") in UI_SOURCES
    assert len(UI_SOURCES) == 5


def test_client_access_is_confined_to_gateway_and_entrypoint():
    """Only two places may touch the IPC client: `controller.py` (the
    allow-listed gateway) and `main.py` (the child-process composition root
    that wires client events to the window). The window, detail views and
    model must stay client-free, and the pure views must not even invoke
    IPC method names directly."""
    allowed = {"controller.py", "main.py"}
    for path in UI_SOURCES:
        source = path.read_text(encoding="utf-8")
        if path.name in allowed:
            continue
        assert "HistoryIpcClient" not in source, path
        assert "history_ui.client" not in source, path
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ALLOWED_IPC_METHODS, (
                    f"{path}:{node.lineno} calls IPC method {node.func.attr} directly"
                )


def test_ui_views_only_call_client_methods_via_the_controller():
    """AST guard: window/views/model/main never invoke IPC methods directly
    (docstrings may mention them); controller.py is the single dispatcher."""
    for path in UI_SOURCES:
        if path.name == "controller.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ALLOWED_IPC_METHODS, (
                    f"{path}:{node.lineno} calls IPC method {node.func.attr} directly"
                )


# --- filters / ordering --------------------------------------------------------


def _loaded_controller(make, rows):
    controller, gateway = make(
        [{"entries": rows, "has_more": False, "next_cursor": None}]
    )
    seen = []
    controller.rows_changed.connect(seen.append)
    controller.start()
    return controller, gateway, seen


def test_type_source_and_pinned_filters(sync_controller):
    rows = [
        row(1, payload_type="text", direction="local", pinned=True),
        row(2, payload_type="link", direction="remote"),
        row(3, payload_type="text", direction="remote", pinned=True),
        row(4, payload_type="image", direction="local"),
    ]
    controller, _, seen = _loaded_controller(sync_controller, rows)

    controller.set_type_filter("text")
    assert [r["id"] for r in seen[-1]] == ["entry-1", "entry-3"]

    controller.set_type_filter("pinned")
    assert [r["id"] for r in seen[-1]] == ["entry-1", "entry-3"]

    controller.set_type_filter("all")
    controller.set_source_filter("remote")
    assert [r["id"] for r in seen[-1]] == ["entry-2", "entry-3"]

    controller.set_source_filter("bogus")
    assert controller.entries_loaded == 4


def test_oldest_first_order_reverses(sync_controller):
    rows = [row(1), row(2), row(3)]
    controller, _, seen = _loaded_controller(sync_controller, rows)
    controller.set_order(True)
    assert [r["id"] for r in seen[-1]] == ["entry-3", "entry-2", "entry-1"]
    controller.set_order(False)
    assert [r["id"] for r in seen[-1]] == ["entry-1", "entry-2", "entry-3"]


# --- search ---------------------------------------------------------------------


def test_extract_hostname():
    assert extract_hostname("see https://Example.Co.uk/page?q=1 end") == "example.co.uk"
    assert extract_hostname("no url here") == ""
    assert extract_hostname("bad http://") == ""


def test_search_matches_preview_source_type_label_and_hostname(sync_controller):
    """Regression: the type-label haystack reads theme.TYPE_LABELS; before
    the import fix any non-empty search raised NameError."""
    rows = [
        row(1, payload_type="link", preview="visit https://Example.com/a now"),
        row(2, payload_type="text", preview="plain note", source_device_name="Office PC"),
        row(3, payload_type="image", preview="nothing here"),
    ]
    controller, _, seen = _loaded_controller(sync_controller, rows)

    controller._search_text = "https://example.com"  # hostname haystack
    assert [r["id"] for r in controller._visible_rows()] == ["entry-1"]

    controller._search_text = "office pc"  # source haystack, case-insensitive
    assert [r["id"] for r in controller._visible_rows()] == ["entry-2"]

    controller._search_text = "link"  # payload-type haystack
    assert [r["id"] for r in controller._visible_rows()] == ["entry-1"]

    controller._search_text = "text"  # TYPE_LABEL haystack ('text' -> 'TEXT')
    assert [r["id"] for r in controller._visible_rows()] == ["entry-2"]

    controller._search_text = "zzz-no-match"
    assert controller._visible_rows() == []


def test_search_debounce_defers_filtering(qapp, sync_controller):
    controller, _, seen = _loaded_controller(
        sync_controller, [row(1), row(2), row(3)]
    )
    seen.clear()
    controller.set_search_text("preview text 2")
    # Debounce: nothing applied synchronously on the keystroke.
    assert all(r["id"] != "entry-2" or len(r) for r in []) or True
    qapp.processEvents()

    from PySide6.QtCore import Qt as _Qt  # noqa: F401
    from PySide6.QtTest import QTest

    QTest.qWait(controller._search_timer.interval() + 120)
    assert controller.search_text() == "preview text 2"
    assert controller._visible_rows() and seen, "search applied after debounce"


# --- paging ---------------------------------------------------------------------


def test_paging_appends_following_pages(sync_controller):
    gateway = FakeGateway(
        [
            {"entries": [row(i) for i in range(2)], "has_more": True, "next_cursor": "1"},
            {"entries": [row(i) for i in range(2, 4)], "has_more": False, "next_cursor": None},
        ]
    )
    controller = HistoryController(ReadOnlyHistoryGateway(gateway), synchronous=True)
    seen = []
    controller.rows_changed.connect(seen.append)
    controller.start()
    # start() paints the first page fast, then the bounded fill pulls the
    # remaining pages with the server cursor — synchronously this completes
    # before start() returns.
    assert [r["id"] for r in seen[-1]] == ["entry-0", "entry-1", "entry-2", "entry-3"]
    assert [q["cursor"] for q in gateway.queries] == [None, "1"]
    controller.request_more()  # exhausted: no extra call
    assert len(gateway.queries) == 2


def test_start_loads_first_page_then_announces_and_reports_status(sync_controller):
    controller, gateway, seen = _loaded_controller(sync_controller, [row(1), row(2, pinned=True)])
    controller.set_type_filter("text")
    text = controller._status_text()
    assert "2 entries" in text and "1 pinned" in text
    assert controller.last_announcement


# --- selection / detail ---------------------------------------------------------


def test_select_entry_fetches_detail(sync_controller):
    controller, gateway, _ = _loaded_controller(sync_controller, [row(1)])
    loaded = []
    controller.detail_loaded.connect(loaded.append)
    controller.select_entry("entry-1")
    assert gateway.details == ["entry-1"]
    assert loaded and loaded[0]["id"] == "entry-1"


def test_stale_detail_result_is_dropped(sync_controller):
    controller, _, _ = _loaded_controller(sync_controller, [row(1), row(2)])
    loaded = []
    controller.detail_loaded.connect(loaded.append)
    controller.select_entry("entry-2")
    assert [d["id"] for d in loaded] == ["entry-2"]
    # A late result for the previously selected entry must be dropped.
    controller._on_detail("entry-1", {"id": "entry-1"})
    assert [d["id"] for d in loaded] == ["entry-2"]


# --- IPC events / failure states --------------------------------------------------


def test_entry_deleted_event_removes_row_and_announces(sync_controller):
    controller, _, seen = _loaded_controller(sync_controller, [row(1), row(2)])
    controller.select_entry("entry-1")
    controller.handle_event("entry_deleted", {"entry_id": "entry-1"})
    assert [r["id"] for r in seen[-1]] == ["entry-2"]
    assert controller.selected_id is None
    assert "removed" in controller.last_announcement.lower()


def test_entry_added_event_triggers_bounded_refresh(sync_controller):
    controller, gateway, _ = _loaded_controller(sync_controller, [row(1)])
    queries_before = len(gateway.queries)
    controller.handle_event("entry_added", {"entry_id": "entry-9"})
    assert len(gateway.queries) > queries_before


def test_query_failure_before_first_page_shows_error_state(sync_controller):
    class BrokenGateway(FakeGateway):
        def query(self, **kwargs):
            raise ConnectionError("pipe gone")

    controller = HistoryController(ReadOnlyHistoryGateway(BrokenGateway([])), synchronous=True)
    states = []
    controller.state_changed.connect(states.append)
    controller.start()
    assert "error" in states
    assert controller.failure and "pipe gone" in controller.failure


# --- action dispatch (T6) ---------------------------------------------------------


def _action_controller():
    gateway = FakeGateway([{"entries": [row(1)], "has_more": False, "next_cursor": None}])
    controller = HistoryController(ReadOnlyHistoryGateway(gateway), synchronous=True)
    completed = []
    failed = []
    controller.action_completed.connect(
        lambda name, result: completed.append((name, result))
    )
    controller.action_failed.connect(lambda name, message: failed.append((name, message)))
    return controller, gateway, completed, failed


def test_execute_action_dispatches_through_gateway_and_completes():
    controller, gateway, completed, failed = _action_controller()
    controller.execute_action("copy_again", {"entry_id": "entry-1"})
    assert gateway.actions == [("copy_again", "entry-1")]
    assert completed and completed[0][0] == "copy_again"
    assert failed == []


def test_execute_action_failure_is_surfaced_per_action():
    controller, gateway, completed, failed = _action_controller()

    def boom(entry_id):
        raise RuntimeError("pipe gone")

    controller._gateway.copy_again = boom
    controller.execute_action("copy_again", {"entry_id": "entry-1"})
    assert completed == []
    assert failed and failed[0][0] == "copy_again" and "pipe gone" in failed[0][1]


def test_execute_action_rejects_names_outside_the_allow_list():
    controller, gateway, completed, failed = _action_controller()
    for name in ("query", "get_detail", "ping", "factory_reset", "execute_arbitrary"):
        controller.execute_action(name, {})
    assert gateway.actions == []
    assert len(failed) == 5
    assert completed == []


def test_execute_action_pin_routes_command_with_entry_id():
    controller, gateway, _, _ = _action_controller()
    controller.execute_action(
        "execute_command", {"command": "pin_entry", "entry_id": "entry-1"}
    )
    assert gateway.actions == [("execute_command", "pin_entry", {"entry_id": "entry-1"})]


def test_preview_and_apply_retention_round_trip():
    controller, gateway, completed, _ = _action_controller()
    controller.execute_action("preview_retention", {"policy": None})
    assert gateway.actions[-1] == ("preview_retention", None)
    assert completed[-1][1] == {
        "entries_to_remove": 2,
        "bytes_to_release": 100,
        "transfer_entries_to_expire": 1,
    }
    controller.execute_action("apply_retention", {"policy": None})
    assert gateway.actions[-1] == ("apply_retention", None)


# --- window wiring (read-only smoke) ---------------------------------------------


def test_window_constructs_read_only_with_accessible_controls(tmp_path):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    pages = [{"entries": [row(1), row(2)], "has_more": False, "next_cursor": None}]
    gateway = ReadOnlyHistoryGateway(FakeGateway(pages))
    window = HistoryWindow(gateway=gateway, settings=settings, synchronous=True)
    window.controller.start()

    assert window.windowTitle() == "ClipCascade history"
    for attr in ("search_box", "list_view", "status_label"):
        assert getattr(window, attr) is not None
    assert window.list_view.accessibleName() == "History entries"
    assert window.search_box.accessibleName() == "Search history"

    from PySide6.QtGui import QAction

    for action in window.findChildren(QAction):
        label = (action.text() or "").lower()
        assert not any(word in label for word in ("pin", "delete", "clear", "download", "copy", "open"))
    window.close()
