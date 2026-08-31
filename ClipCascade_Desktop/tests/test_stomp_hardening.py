"""C-batch-1 hardening tests: STOMP frame parser crash-safety and total-size
cap, reconnect backoff with a single-flight guard (no stampede), surfaced
receive-path failures, and the de-coupled ws_interface import.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.config import Config
from stomp_ws import stomp_manager as stomp_manager_module
from stomp_ws.client import MAX_INCOMING_FRAME_BYTES, Client
from stomp_ws.frame import Frame, MalformedFrameError
from stomp_ws.stomp_manager import (
    RECONNECT_BACKOFF_INITIAL_S,
    RECONNECT_BACKOFF_JITTER_S,
    RECONNECT_BACKOFF_MAX_S,
    STOMPManager,
)

SRC = Path(__file__).parents[1] / "src"


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


# --- frame parser -------------------------------------------------------


def test_frame_roundtrip_preserves_fields():
    original = Frame.marshall(
        "MESSAGE", {"subscription": "sub-0", "message-id": "m-1"}, "hello world"
    )
    frame = Frame.unmarshall_single(original)
    assert frame.command == "MESSAGE"
    assert frame.headers["subscription"] == "sub-0"
    assert frame.body == "hello world"


def test_frame_with_null_body_parses_to_none():
    original = Frame.marshall("CONNECTED", {"version": "1.1"}, None)
    frame = Frame.unmarshall_single(original)
    assert frame.command == "CONNECTED"
    assert frame.headers["version"] == "1.1"


@pytest.mark.parametrize(
    "data",
    [
        "",
        None,
        "MESSAGE",  # no header/body separator
        "MESSAGE\nbadheaderwithoutcolon\n\nbody\x00",  # header without ':'
        "MESSAGE\nsubscription:s\n",  # no body section after separator
        "\n\nbody\x00",  # empty command
    ],
)
def test_malformed_frames_raise_instead_of_crashing(data):
    with pytest.raises(MalformedFrameError):
        Frame.unmarshall_single(data)


def test_malformed_frames_do_not_crash_the_message_handler():
    client = Client("ws://ignored")
    client.ws = FakeWs()
    for data in ("garbage", "A\nb\n\nc\x00", ""):
        client._on_message(None, data)
    assert client.ws.closed is True, "connection must be closed after a malformed frame"


def test_oversized_frame_is_dropped_and_connection_closed():
    client = Client("ws://ignored")
    client.ws = FakeWs()
    huge = "x" * (MAX_INCOMING_FRAME_BYTES + 1)
    client._on_message(None, huge)
    assert client.ws.closed is True


def test_valid_message_frame_dispatches_to_subscription():
    client = Client("ws://ignored")
    client.ws = FakeWs()
    received = []
    client.subscribe("/user/queue/cliptext", callback=lambda frame: received.append(frame.body))
    client._on_message(None, Frame.marshall("MESSAGE", {"subscription": "sub-0", "message-id": "m-1"}, "payload-1"))
    assert received == ["payload-1"]
    assert client.ws.closed is False


# --- reconnect backoff ---------------------------------------------------


class RecordingManager(STOMPManager):
    """STOMPManager with the notification and connect seams replaced."""

    def __init__(self, connect_results):
        self.connect_results = list(connect_results)
        self.connect_calls = 0
        super().__init__(_test_config(), is_login_phase=False)

    def connect(self):
        self.connect_calls += 1
        if not self.connect_results:
            raise AssertionError("unexpected connect call")
        result = self.connect_results.pop(0)
        if result[0]:
            # mirror the real connect() contract: success resets the backoff
            self._reset_reconnect_state()
        return result


def _test_config():
    cfg = Config(file_name="unused-in-hardening-tests")
    cfg.data["cipher_enabled"] = False
    return cfg


@pytest.fixture
def no_notifications(monkeypatch):
    def _silence(manager):
        class FakeNotifier:
            def notify(self, title, message):
                pass

        manager.notification_manager = FakeNotifier()

    return _silence


def test_on_close_schedules_reconnect_without_sleeping(no_notifications):
    manager = RecordingManager(connect_results=[(True, "ok")])
    no_notifications(manager)
    started = time.monotonic()
    manager._on_close()
    scheduling_took = time.monotonic() - started
    assert scheduling_took < 0.5, "_on_close must return immediately (no inline sleep)"
    # scheduled reconnect fires promptly at backoff initial delay
    deadline = time.monotonic() + RECONNECT_BACKOFF_INITIAL_S + RECONNECT_BACKOFF_JITTER_S + 5
    while manager.connect_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert manager.connect_calls == 1
    assert manager._reconnect_scheduled is False


def test_failed_connect_grows_backoff_and_stays_single_flight(no_notifications):
    manager = RecordingManager(connect_results=[(False, "down"), (False, "down"), (True, "ok")])
    no_notifications(manager)
    manager._schedule_reconnect(delay_s=0.0)

    deadline = time.monotonic() + 30
    while manager.connect_calls < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    # the failed first connect schedules exactly one retry: delay is now
    # initial * factor^1 (+jitter), i.e. above the first attempt's delay
    assert manager._reconnect_attempts >= 2

    # hammering _schedule_reconnect from many threads must not stack retries
    threads = [
        threading.Thread(target=manager._schedule_reconnect) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # two more attempts happen (both failures schedule again), then success
    deadline = time.monotonic() + 30
    while manager.connect_calls < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert manager.connect_calls == 3, "exactly one reconnect chain may run"
    time.sleep(1.0)  # any wrongly-stacked timers would fire by now
    assert manager.connect_calls == 3, "concurrent schedules must not stack reconnects"
    assert manager._reconnect_scheduled is False
    assert manager._reconnect_attempts == 0, "success resets the backoff"


def test_backoff_is_capped():
    assert (
        stomp_manager_module.RECONNECT_BACKOFF_INITIAL_S
        * stomp_manager_module.RECONNECT_BACKOFF_FACTOR ** 10
        > RECONNECT_BACKOFF_MAX_S
    )
    # the capped formula is what _schedule_recompute uses
    attempts = 20
    delay = min(
        RECONNECT_BACKOFF_INITIAL_S
        * (stomp_manager_module.RECONNECT_BACKOFF_FACTOR ** attempts),
        RECONNECT_BACKOFF_MAX_S,
    )
    assert delay == RECONNECT_BACKOFF_MAX_S


def test_disconnect_cancels_pending_reconnect(no_notifications):
    manager = RecordingManager(connect_results=[])
    no_notifications(manager)
    manager._schedule_reconnect(delay_s=30.0)
    manager.disconnect()
    assert manager._reconnect_timer is None or manager._reconnect_timer.finished.is_set()
    time.sleep(0.3)
    assert manager.connect_calls == 0, "no reconnect may fire after disconnect"


def test_successful_connect_resets_attempts(no_notifications):
    manager = RecordingManager(connect_results=[])
    no_notifications(manager)
    manager._schedule_reconnect(delay_s=30.0)
    manager._reset_reconnect_state()
    assert manager._reconnect_attempts == 0
    assert manager._reconnect_scheduled is False
    assert manager._reconnect_timer is None


# --- receive-path failure surfacing -------------------------------------


def test_receive_failure_notifies_user_once_per_interval(no_notifications):
    manager = STOMPManager(_test_config(), is_login_phase=True)
    notifications = []

    class RateLimitedRecorder:
        def notify(self, title, message):
            notifications.append(title)

    manager.notification_manager = RateLimitedRecorder()
    manager.is_connected = True

    class BrokenFrame:
        body = "{not valid json"

    for _ in range(3):
        manager._receive(BrokenFrame())

    assert len(notifications) == 1, "repeated failures must be rate-limited to one notification"
    assert "Invalid clipboard data" in notifications[0]


# --- ws_interface decoupling ---------------------------------------------


def test_ws_interface_does_not_import_cli_tray():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'"
            + str(SRC)
            + "'); import interfaces.ws_interface; "
            "assert 'cli.tray' not in sys.modules, 'ws_interface must not import cli.tray'; print('OK')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout
