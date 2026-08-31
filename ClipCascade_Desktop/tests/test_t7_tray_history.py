"""T7: tray integration and open-history degradation.

Covers the tray menu contract added on top of the existing TaskbarPanel:
- an "Open history" entry exists exactly when a history callback is wired;
- double-click (the pystray default action) opens history only when no
  pending-file download is armed -- the download shortcut keeps precedence;
- every failure on the open-history path degrades to a log line and never
  escapes into the tray or the application.
"""

import logging

import pytest

from core.application import Application
from gui.tray import TaskbarPanel

HISTORY_TEXT = "🕘 Open history"
DOWNLOAD_TEXT = "📥 Download File(s)"
CONNECT_TEXT = "🔗 Connect"


def _noop_handler(icon, item):
    pass


def _panel(**overrides) -> TaskbarPanel:
    """A TaskbarPanel without __init__ (no Tk/pystray mainloop needed):
    create_menu() only reads the state attributes below."""
    panel = TaskbarPanel.__new__(TaskbarPanel)
    panel.is_connected = True
    panel.is_disconnecting = False
    panel.disconnecting_items = None
    panel.new_version_available = None
    panel.is_file_download_enabled = False
    panel.file_download_items = None
    panel.previous_stats_items = None
    panel.on_open_history_callback = lambda: None
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _texts(panel) -> list:
    return [i.text for i in panel.create_menu().items]


def _first_default(panel):
    """pystray's double-click rule: the first default item wins."""
    for menu_item in panel.create_menu().items:
        if menu_item.default:
            return menu_item.text
    return None


def _armed_download(panel):
    panel.is_file_download_enabled = True
    panel.file_download_items = (DOWNLOAD_TEXT, 0, _noop_handler)


# --- tray menu ---------------------------------------------------------------


def test_open_history_item_present_when_callback_wired():
    assert HISTORY_TEXT in _texts(_panel())


def test_no_history_item_without_callback():
    assert HISTORY_TEXT not in _texts(_panel(on_open_history_callback=None))


def test_double_click_opens_history_when_no_pending_download():
    assert _first_default(_panel()) == HISTORY_TEXT


def test_double_click_keeps_pending_download_precedence():
    panel = _panel()
    _armed_download(panel)
    assert _first_default(panel) == DOWNLOAD_TEXT
    history_item = next(
        i for i in panel.create_menu().items if i.text == HISTORY_TEXT
    )
    assert not history_item.default  # never competes with the download action


def test_history_item_sits_above_logs_and_below_download():
    panel = _panel()
    _armed_download(panel)
    texts = _texts(panel)
    assert texts.index(DOWNLOAD_TEXT) < texts.index(HISTORY_TEXT)
    assert texts.index(HISTORY_TEXT) < texts.index("🗒️ Open Logs")


def test_disconnect_keeps_double_click_when_disconnected():
    panel = _panel(is_connected=False)
    assert _first_default(panel) == CONNECT_TEXT


def test_update_available_keeps_history_menu_intact():
    panel = _panel(new_version_available=[True, "9.9", "3.2.0", "https://x"])
    texts = _texts(panel)
    assert HISTORY_TEXT in texts
    assert _first_default(panel) == HISTORY_TEXT


# --- open-history handler containment ----------------------------------------


def test_open_history_without_callback_is_a_noop():
    panel = _panel(on_open_history_callback=None)
    panel._on_open_history(None, None)  # must not raise


def test_open_history_callback_failure_is_contained(caplog):
    def broken():
        raise RuntimeError("launcher exploded")

    panel = _panel(on_open_history_callback=broken)
    with caplog.at_level(logging.ERROR):
        panel._on_open_history(None, None)  # must not raise
    assert any("history window" in r.getMessage().lower() for r in caplog.records)


# --- application open path (dead launcher degrades gracefully) ----------------


def _app_with_launcher(launcher) -> Application:
    app = Application.__new__(Application)
    app.history_launcher = launcher
    return app


class _WorkingLauncher:
    def open_or_focus(self):
        return "launched"


class _DeadLauncher:
    def open_or_focus(self):
        raise OSError("pipe is gone")


def test_open_history_without_launcher_logs_and_returns(caplog):
    app = _app_with_launcher(None)
    with caplog.at_level(logging.INFO):
        app._open_history_window()  # must not raise
    assert any("not active" in r.getMessage() for r in caplog.records)


def test_open_history_with_dead_launcher_logs_and_returns(caplog):
    app = _app_with_launcher(_DeadLauncher())
    with caplog.at_level(logging.ERROR):
        app._open_history_window()  # must not raise
    assert any(
        isinstance(r.exc_info, tuple) and r.exc_info[0] is OSError
        for r in caplog.records
    )


def test_open_history_with_working_launcher_reports_outcome(caplog):
    app = _app_with_launcher(_WorkingLauncher())
    with caplog.at_level(logging.INFO):
        app._open_history_window()
    assert any("launched" in r.getMessage() for r in caplog.records)


def test_history_notice_failure_never_escapes(monkeypatch):
    from utils import notification_manager as nm

    def explode(self, title, message, **kwargs):
        raise RuntimeError("no notifications here")

    monkeypatch.setattr(nm.NotificationManager, "notify", explode)
    app = Application.__new__(Application)
    app.config = type("Cfg", (), {"data": {"notification": True}})()
    app._notify_history_notice("anything")  # must not raise


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
