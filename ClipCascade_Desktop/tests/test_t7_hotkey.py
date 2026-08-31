"""T7: opt-in Ctrl+Alt+V history hotkey (win32 seams faked).

Verifies the `history/hotkey_win.py` lifecycle without touching the real
RegisterHotKey/UnregisterHotKey APIs or the real clipboard monitor window:
registration inputs (Ctrl+Alt|V on the hidden window's hwnd), collision
handling (one notice, session-inactive, tray path unaffected), guaranteed
unregister on window destruction, re-registration after window recreation,
the single-flight WM_HOTKEY dispatch, and shutdown.
"""

import logging
import threading
import time

import pytest
import win32con

from history import hotkey_win

HISTORY_NOTICE = (
    "The Ctrl+Alt+V history shortcut could not be registered "
    "(it may be in use by another application). "
    "History remains available from the ClipCascade tray menu."
)


class FakeWin32:
    """Records Register/Unregister calls; can be told to collide."""

    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.fail_next = False

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, virtual_key):
        if self.fail_next:
            raise OSError(1409, "RegisterHotKey", "Hot key is already registered.")
        self.registered.append((hwnd, hotkey_id, modifiers, virtual_key))

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.unregistered.append((hwnd, hotkey_id))


class FakeMonitor:
    def __init__(self):
        self.setup_requests = 0
        self.stops = 0

    def request_hotkey_setup(self):
        self.setup_requests += 1

    def stop(self):
        self.stops += 1


@pytest.fixture
def win32(monkeypatch):
    fake = FakeWin32()
    monkeypatch.setattr(hotkey_win.win32gui, "RegisterHotKey", fake.RegisterHotKey)
    monkeypatch.setattr(hotkey_win.win32gui, "UnregisterHotKey", fake.UnregisterHotKey)
    return fake


@pytest.fixture
def monitor(monkeypatch):
    fake = FakeMonitor()
    monkeypatch.setattr(hotkey_win, "_monitor", fake)
    return fake


@pytest.fixture(autouse=True)
def reset_state():
    yield
    hotkey_win._enabled_config = False
    hotkey_win._session_active = False
    hotkey_win._notice_shown = False
    hotkey_win._trigger = None
    hotkey_win._notice = None
    hotkey_win._registered_hwnd = None
    hotkey_win._dispatch_in_flight = False


class _Config:
    def __init__(self, enabled):
        self.data = {"enable_history_hotkey": enabled}


def _enable(win32, monitor, enabled=True, trigger=None, notice=None):
    calls = []

    def default_trigger():
        calls.append("trigger")

    hotkey_win.enable(
        _Config(enabled),
        on_trigger=trigger or default_trigger,
        on_notice=notice,
    )
    return calls


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- enablement ---------------------------------------------------------------


def test_disabled_by_default_registers_nothing(win32, monitor):
    _enable(win32, monitor, enabled=False)
    hotkey_win.on_window_ready(1234)
    assert win32.registered == []
    assert monitor.setup_requests == 0


def test_enabled_setting_requests_setup_on_the_existing_window(win32, monitor):
    _enable(win32, monitor, enabled=True)
    assert monitor.setup_requests == 1


def test_window_ready_registers_ctrl_alt_v(win32, monitor):
    _enable(win32, monitor, enabled=True)
    hotkey_win.on_window_ready(555)
    assert win32.registered == [
        (555, hotkey_win.HOTKEY_ID, win32con.MOD_CONTROL | win32con.MOD_ALT, ord("V"))
    ]
    assert hotkey_win._session_active is True


def test_window_ready_is_idempotent_while_active(win32, monitor):
    _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_window_ready(2)
    assert len(win32.registered) == 1
    assert hotkey_win._registered_hwnd == 1


# --- collision / failure --------------------------------------------------------


def test_collision_shows_exactly_one_notice_and_stays_inactive(
    win32, monitor, caplog
):
    notices = []
    win32.fail_next = True
    _enable(win32, monitor, notice=notices.append)

    hotkey_win.on_window_ready(1)
    hotkey_win.on_window_ready(1)  # a recreated window that fails again
    with caplog.at_level(logging.WARNING):
        hotkey_win.on_window_ready(1)

    assert notices == [HISTORY_NOTICE]  # exactly one user-visible notice
    assert hotkey_win._session_active is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3  # every failure is logged, only one is notified


def test_collision_never_fires_the_trigger(win32, monitor):
    calls = _enable(win32, monitor)
    win32.fail_next = True
    hotkey_win.on_window_ready(1)
    hotkey_win.on_hotkey()
    assert calls == []


def test_failure_then_success_after_window_recreation(win32, monitor):
    notices = []
    _enable(win32, monitor, notice=notices.append)
    win32.fail_next = True
    hotkey_win.on_window_ready(1)
    win32.fail_next = False
    hotkey_win.on_window_closing(1)  # monitor restart path always closes first
    hotkey_win.on_window_ready(2)
    assert hotkey_win._session_active is True
    assert hotkey_win._registered_hwnd == 2
    assert len(notices) == 1


# --- WM_HOTKEY dispatch ---------------------------------------------------------


def test_hotkey_dispatches_trigger_once(win32, monitor):
    calls = _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_hotkey()
    assert _wait_for(lambda: len(calls) == 1)


def test_presses_while_dispatch_in_flight_coalesce(win32, monitor):
    calls = _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    release = threading.Event()

    def slow_trigger():
        release.wait(2)
        calls.append("trigger")

    hotkey_win._trigger = slow_trigger
    hotkey_win.on_hotkey()
    assert _wait_for(lambda: hotkey_win._dispatch_in_flight)
    hotkey_win.on_hotkey()  # arrives while the first dispatch runs: dropped
    release.set()
    assert _wait_for(lambda: calls == ["trigger"])
    assert hotkey_win._dispatch_in_flight is False


def test_hotkey_ignored_before_registration(win32, monitor):
    calls = _enable(win32, monitor)
    hotkey_win.on_hotkey()
    time.sleep(0.05)
    assert calls == []


def test_trigger_exception_is_contained(win32, monitor):
    def broken():
        raise RuntimeError("no launcher")

    _enable(win32, monitor, trigger=broken)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_hotkey()
    assert _wait_for(lambda: hotkey_win._dispatch_in_flight is False)


# --- unregister guarantees ------------------------------------------------------


def test_window_closing_unregisters_and_disables(win32, monitor):
    calls = _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_window_closing(1)
    assert win32.unregistered == [(1, hotkey_win.HOTKEY_ID)]
    hotkey_win.on_hotkey()
    time.sleep(0.05)
    assert calls == []
    assert hotkey_win._session_active is False


def test_closing_with_unknown_hwnd_is_ignored(win32, monitor):
    _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_window_closing(999)
    assert win32.unregistered == []
    assert hotkey_win._session_active is True


def test_recreated_window_re_registers(win32, monitor):
    _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.on_window_closing(1)
    hotkey_win.on_window_ready(2)
    assert [r[0] for r in win32.registered] == [1, 2]
    assert hotkey_win._session_active is True


def test_shutdown_resets_state_and_stops_monitor(win32, monitor):
    calls = _enable(win32, monitor)
    hotkey_win.on_window_ready(1)
    hotkey_win.shutdown()
    assert monitor.stops == 1
    assert hotkey_win._enabled_config is False
    hotkey_win.on_hotkey()
    time.sleep(0.05)
    assert calls == []


# --- clipboard monitor seams (real module, no real messages pumped) --------------


def test_monitor_wndproc_routes_hotkey_and_setup_messages():
    from clipboard import clipboard_monitor_win as monitor_win

    events = []
    monitor_win.set_hotkey_callbacks(
        on_window_ready=lambda hwnd: events.append(("ready", hwnd)),
        on_window_closing=lambda hwnd: events.append(("closing", hwnd)),
        on_hotkey=lambda: events.append(("hotkey", None)),
    )
    try:
        monitor_win._process_message(1, monitor_win.WM_HOTKEY, hotkey_win.HOTKEY_ID, 0)
        monitor_win._process_message(1, monitor_win._WM_APP_HOTKEY_READY, 0, 0)
    finally:
        monitor_win.set_hotkey_callbacks()
    assert events == [("hotkey", None), ("ready", 1)]


def test_monitor_request_hotkey_setup_is_noop_without_window():
    from clipboard import clipboard_monitor_win as monitor_win

    assert monitor_win._hwnd is None  # nothing started in this process
    monitor_win.request_hotkey_setup()  # must not raise


def test_hotkey_constants_are_stable():
    from clipboard import clipboard_monitor_win as monitor_win

    # One WM_HOTKEY path: the monitor owns 0x0312 and the hotkey id stays
    # inside the app-private range.
    assert monitor_win.WM_HOTKEY == 0x0312
    assert 0x0000 <= hotkey_win.HOTKEY_ID <= 0xBFFF


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
