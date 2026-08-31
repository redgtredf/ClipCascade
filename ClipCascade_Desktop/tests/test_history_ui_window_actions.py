"""T6 window/dialog tests: state-appropriate actions, destructive-flow
confirmations, the per-hostname link trust flow and per-action error
surfaces — all against the real window + controller with a fake gateway."""

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402

from history_ui import link_policy  # noqa: E402
from history_ui.controller import ReadOnlyHistoryGateway  # noqa: E402
from history_ui.detail_views import DetailPane  # noqa: E402
from history_ui.window import (  # noqa: E402
    ClearHistoryDialog,
    HistoryWindow,
    LinkConfirmDialog,
    RetentionDialog,
)

from tests.test_history_ui_controller import FakeGateway, row  # noqa: E402


def detail_for(**overrides):
    detail = {
        "id": "entry-1",
        "created_at_utc": "2026-08-31T10:00:00+00:00",
        "updated_at_utc": "2026-08-31T10:00:00+00:00",
        "payload_type": "text",
        "direction": "local",
        "transport": "local",
        "pinned": False,
        "byte_size": 100,
        "preview": "plain text",
        "source_device_name": None,
        "file_state": None,
        "file_count": None,
        "image_width": None,
        "image_height": None,
        "text": "plain text",
        "url": None,
        "image_base64": None,
        "files": [],
        "downloaded_directory": None,
    }
    detail.update(overrides)
    return detail


def _pane_buttons(pane):
    from PySide6.QtWidgets import QPushButton

    return {button.text(): button for button in pane.findChildren(QPushButton)}


def _window(pages, tmp_path):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    fake = FakeGateway(pages)
    gateway = ReadOnlyHistoryGateway(fake)
    window = HistoryWindow(gateway=gateway, settings=settings, synchronous=True)
    window.show()
    window.controller.start()
    return window, fake, settings


def test_detail_actions_per_payload_type(qtbot):
    pane = DetailPane()

    pane.show_detail(detail_for(payload_type="text"))
    buttons = set(_pane_buttons(pane))
    assert {"Copy again", "Pin", "Delete"} <= buttons

    pane.show_detail(detail_for(payload_type="link", url="https://example.com", preview="https://example.com"))
    buttons = set(_pane_buttons(pane))
    assert {"Copy link", "Open in browser…", "Pin", "Delete"} <= buttons

    pane.show_detail(
        detail_for(
            payload_type="image",
            image_base64="iVBORw0KGgo=",
            image_width=4,
            image_height=4,
            preview="Image",
        )
    )
    assert {"Copy again", "Save as…", "Delete"} <= set(_pane_buttons(pane))

    # Corrupt image (undecodable/absent preview): metadata + Delete only.
    pane.show_detail(detail_for(payload_type="image", preview="Image"))
    buttons = set(_pane_buttons(pane))
    assert "Copy again" not in buttons and "Save as…" not in buttons
    assert "Delete" in buttons

    pane.show_detail(detail_for(payload_type="files", file_state="ready", files=[{"name": "a.txt", "size_bytes": 1}], preview="a.txt"))
    assert {"Download all…", "Save…", "Pin", "Delete"} <= set(_pane_buttons(pane))

    pane.show_detail(
        detail_for(
            payload_type="files",
            file_state="downloaded",
            downloaded_directory="C:/downloads",
            files=[{"name": "a.txt", "size_bytes": 1}],
            preview="a.txt",
        )
    )
    assert {"Open folder", "Copy file paths", "Delete"} <= set(_pane_buttons(pane))

    # Expired/unavailable: no download actions at all.
    for state in ("expired", "unavailable"):
        pane.show_detail(detail_for(payload_type="files", file_state=state, preview="a.txt"))
        buttons = set(_pane_buttons(pane))
        assert "Download all…" not in buttons and "Open folder" not in buttons
        assert "Delete" in buttons

    pane.show_detail(detail_for(pinned=True))
    assert "Unpin" in set(_pane_buttons(pane))


def test_copy_again_button_dispatches_and_announces(qtbot, tmp_path):
    window, gateway, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    window.detail_pane.show_detail(detail_for(payload_type="text"))
    window._selected_entry_id = lambda: "entry-1"
    qtbot.addWidget(window)

    _pane_buttons(window.detail_pane)["Copy again"].click()
    assert gateway.actions == [("copy_again", "entry-1")]
    assert window._last_announcement == "Copied to the clipboard."


def test_delete_requires_confirmation(qtbot, tmp_path, monkeypatch):
    window, gateway, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    window.detail_pane.show_detail(detail_for(payload_type="text"))
    window._selected_entry_id = lambda: "entry-1"
    qtbot.addWidget(window)

    monkeypatch.setattr(window, "_confirm_delete", lambda: False)
    _pane_buttons(window.detail_pane)["Delete"].click()
    assert gateway.actions == []  # cancelled -> no state change

    monkeypatch.setattr(window, "_confirm_delete", lambda: True)
    _pane_buttons(window.detail_pane)["Delete"].click()
    assert gateway.actions == [("execute_command", "delete_entry", {"entry_id": "entry-1"})]


def test_pin_unpin_routing(qtbot, tmp_path):
    window, gateway, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    window.detail_pane.show_detail(detail_for(payload_type="text", pinned=False))
    window._selected_entry_id = lambda: "entry-1"
    qtbot.addWidget(window)
    _pane_buttons(window.detail_pane)["Pin"].click()
    assert gateway.actions[-1] == ("execute_command", "pin_entry", {"entry_id": "entry-1"})

    window.detail_pane.show_detail(detail_for(payload_type="text", pinned=True))
    _pane_buttons(window.detail_pane)["Unpin"].click()
    assert gateway.actions[-1] == ("execute_command", "unpin_entry", {"entry_id": "entry-1"})


def test_link_flow_confirms_then_trusts(qtbot, tmp_path, monkeypatch):
    window, gateway, settings = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)
    opened = []
    monkeypatch.setattr("history_ui.window.QDesktopServices.openUrl", lambda url: opened.append(url.toString()))
    dialogs = []

    class FakeDialog(LinkConfirmDialog):
        def __init__(self, check, parent=None):
            super().__init__(check, parent)
            dialogs.append(self)

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("history_ui.window.LinkConfirmDialog", FakeDialog)

    window._open_link("https://example.com/page")
    assert len(dialogs) == 1  # untrusted hostname -> confirmation shown
    assert opened == ["https://example.com/page"]
    assert not link_policy.is_trusted(settings, "example.com")  # checkbox untouched

    window._open_link("https://example.com/other")
    assert len(dialogs) == 2

    # Grant trust (checkbox on) -> dialog then never again for this hostname.
    FakeDialog.exec = lambda self: (
        self._trust_checkbox.setChecked(True),
        QDialog.DialogCode.Accepted,
    )[1]
    window._open_link("https://example.com/third")
    assert link_policy.is_trusted(settings, "example.com")
    dialogs.clear()
    window._open_link("https://example.com/fourth")
    assert dialogs == []  # trusted: no confirmation
    assert opened[-1] == "https://example.com/fourth"


def test_link_flow_http_always_warns_even_if_trust_requested(qtbot, tmp_path, monkeypatch):
    window, _, settings = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)
    monkeypatch.setattr("history_ui.window.QDesktopServices.openUrl", lambda url: None)
    checks = []

    class FakeDialog(LinkConfirmDialog):
        def __init__(self, check, parent=None):
            super().__init__(check, parent)
            checks.append(check)
            assert not self._trust_checkbox.isEnabled()  # HTTP: trust offer disabled

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("history_ui.window.LinkConfirmDialog", FakeDialog)
    window._open_link("http://insecure.example/path")
    window._open_link("http://insecure.example/again")
    assert len(checks) == 2  # HTTP always warns, even after "trust"
    assert not link_policy.is_trusted(settings, "insecure.example")


def test_link_flow_invalid_link_is_refused_silently(qtbot, tmp_path, monkeypatch):
    window, _, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)
    opened = []
    monkeypatch.setattr("history_ui.window.QDesktopServices.openUrl", lambda url: opened.append(url))
    window._open_link("javascript:alert(1)")
    assert opened == []
    assert "not a valid web link" in window._last_announcement


def test_clear_history_dialog_defaults(qtbot, tmp_path):
    window, _, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)

    dialog = ClearHistoryDialog(window)
    assert dialog.clear_all_selected() is False  # unpinned is the default scope
    assert dialog.clear_clipboard_selected() is False  # clipboard clear is opt-in


def test_clear_history_flow_includes_clipboard_when_selected(qtbot, tmp_path, monkeypatch):
    window, gateway, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)

    class FakeClearDialog(ClearHistoryDialog):
        def exec(self):
            self._unpinned_radio.setCurrentRow(1)  # ALL
            self._clipboard_checkbox.setChecked(True)
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("history_ui.window.ClearHistoryDialog", FakeClearDialog)
    window._on_clear_history()
    kinds = [action[1] for action in gateway.actions if action[0] == "execute_command"]
    assert "clear_all" in kinds
    assert any(action[0] == "clear_windows_clipboard" for action in gateway.actions)


def test_retention_preview_then_apply(qtbot, tmp_path, monkeypatch):
    window, gateway, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)

    class FakeRetentionDialog(RetentionDialog):
        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("history_ui.window.RetentionDialog", FakeRetentionDialog)
    window._on_manage_retention()
    kinds = [action[0] for action in gateway.actions]
    # Synchronous controller: preview -> dialog shown & accepted -> apply,
    # all before _on_manage_retention returns.
    assert "preview_retention" in kinds and "apply_retention" in kinds
    assert kinds.index("preview_retention") < kinds.index("apply_retention")
    assert window._pending_retention_dialog is False


def test_action_failure_surfaces_friendly_message(qtbot, tmp_path):
    window, _, _ = _window(
        [{"entries": [row(1)], "has_more": False, "next_cursor": None}], tmp_path
    )
    qtbot.addWidget(window)
    window._on_action_failed("copy_again", "clipboard-unavailable")
    assert "clipboard is not reachable" in window._last_announcement
    window._on_action_failed("download_files", "invalid-state:expired")
    assert "expired" in window._last_announcement
