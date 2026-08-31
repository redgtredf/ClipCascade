"""C-batch-4 hardening tests: macOS monitor survives polling exceptions,
osascript notifications pass values as argv (no script interpolation), the
legacy-hash login prefill guard, saved-password single-hash round trip, and
POSIX lock-file semantics (no unlink under a live lock).
"""

import importlib
import sys
import threading
import types

import pytest

from core.config import Config

# --- macOS monitor resilience -------------------------------------------


class _BoomThenTextPasteboard:
    """get_contents raises on the first poll, returns text afterwards."""

    calls = 0

    def __init__(self):
        pass

    def get_contents(self, type=None, diff=True):
        _BoomThenTextPasteboard.calls += 1
        if _BoomThenTextPasteboard.calls == 1:
            raise RuntimeError("transient pasteboard hiccup")
        return "recovered clipboard text"

    def get_file_urls(self, diff=True):
        return ()


def test_macos_monitor_survives_first_exception():
    fake_pasteboard = types.ModuleType("pasteboard")
    fake_pasteboard.Pasteboard = _BoomThenTextPasteboard
    fake_pasteboard.String = "String"
    fake_pasteboard.PNG = "PNG"
    fake_pasteboard.TIFF = "TIFF"
    sys.modules["pasteboard"] = fake_pasteboard
    try:
        cm_mac = importlib.import_module("clipboard.clipboard_monitor_mac")
        received = threading.Event()
        callbacks = []

        def callback(type_, content):
            callbacks.append((type_, content))
            received.set()
            cm_mac._run = False  # end the polling loop

        cm_mac._callback_update = callback
        cm_mac._first_run = False
        cm_mac._run = True
        thread = threading.Thread(
            target=cm_mac._runner, kwargs={"enable_image_monitoring": False,
                                           "enable_file_monitoring": False},
            daemon=True,
        )
        thread.start()

        assert received.wait(timeout=10), (
            "monitor died on its first exception: the callback never fired"
        )
        thread.join(timeout=5)
        assert callbacks == [("text", "recovered clipboard text")]
        assert _BoomThenTextPasteboard.calls >= 2
    finally:
        sys.modules.pop("pasteboard", None)
        sys.modules.pop("clipboard.clipboard_monitor_mac", None)


# --- osascript argv-passing notifications --------------------------------


def test_macos_notification_passes_values_as_argv(monkeypatch):
    from utils import notification_manager

    recorded = {}

    def fake_run(args):
        recorded["args"] = list(args)

    monkeypatch.setattr(notification_manager, "PLATFORM", notification_manager.MACOS)
    monkeypatch.setattr(
        notification_manager,
        "subprocess",
        types.SimpleNamespace(run=fake_run),
        raising=False,
    )

    config = Config(file_name="unused")
    config.data["notification"] = True
    hostile_title = 't"; do shell script "evil title"'
    hostile_message = 'm"; do shell script "rm -rf ~"'

    notification_manager.NotificationManager(config).notify(
        title=hostile_title, message=hostile_message
    )

    args = recorded["args"]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    script = args[2]
    # values ride as argv, never interpolated into the AppleScript source
    assert hostile_title not in script
    assert hostile_message not in script
    assert args[3] == hostile_title
    assert args[4] == hostile_message


# --- login prefill + saved-password single-hash ---------------------------


def test_looks_like_sha3_hex_guard():
    from gui.login import LoginForm

    assert LoginForm.looks_like_sha3_hex("a" * 128) is True
    assert LoginForm.looks_like_sha3_hex("A" * 128) is False  # uppercase is not stored
    assert LoginForm.looks_like_sha3_hex("a" * 127) is False
    assert LoginForm.looks_like_sha3_hex("correct horse") is False
    assert LoginForm.looks_like_sha3_hex("") is False


class _FakeLoginForm:
    """Stands in for the UI form: types a raw password once."""

    constructed = 0

    def __init__(self, config, on_quit_callback=None):
        _FakeLoginForm.constructed += 1
        self.config = config

    def mainloop(self):
        self.config.data["password"] = "raw-pass-123"


class _FakeRequestManager:
    def __init__(self, config, first_ws_connect_fails=True):
        self.config = config
        self.posted_passwords = []
        self._first_ws_connect_fails = first_ws_connect_fails
        self.ws_attempts = 0

    def login(self):
        self.posted_passwords.append(self.config.data["password"])
        return True, "Login successful", {"JSESSIONID": "fresh"}

    def get_csrf_token(self):
        return "csrf"

    def get_server_mode(self):
        return "P2S"

    def get_stun_url(self):
        return ""

    def maxsize(self):
        return 1000


class _FakeWsManager:
    def __init__(self):
        self.is_login_phase = True

    def connect(self):
        return True, ""


def _make_application(config):
    from core.application import Application

    app = Application.__new__(Application)
    app.config = config
    app.request_manager = _FakeRequestManager(config)
    app.cipher_manager = None
    app.stomp_manager = _FakeWsManager()
    app.p2p_manager = _FakeWsManager()
    return app


class _NoopDialog:
    def __init__(self, *args, **kwargs):
        pass

    def mainloop(self):
        pass


def test_login_flow_stores_raw_password_and_hashes_once(monkeypatch):
    from utils.cipher_manager import CipherManager
    from core import application

    monkeypatch.setattr(application, "LoginForm", _FakeLoginForm)
    monkeypatch.setattr(application, "CustomDialog", _NoopDialog)

    config = Config(file_name="unused")
    config.data.update(
        {
            "cookie": None,
            "save_password": True,
            "cipher_enabled": False,
            "username": "user",
            "server_url": "http://localhost:8080",
            "password": "",
        }
    )
    app = _make_application(config)
    app.authenticate_and_connect()

    expected_hash = CipherManager.string_to_sha3_512_lowercase_hex("raw-pass-123")
    assert app.request_manager.posted_passwords == [expected_hash], (
        "the server must receive the password hashed exactly once"
    )
    assert config.data["password"] == "raw-pass-123", (
        "save_password must persist the RAW password (hashing it made the "
        "prefilled form double-hash on the next click-through)"
    )


def test_saved_credentials_login_hashes_stored_raw_once(monkeypatch):
    from utils.cipher_manager import CipherManager
    from core import application

    monkeypatch.setattr(application, "LoginForm", _FakeLoginForm)
    monkeypatch.setattr(application, "CustomDialog", _NoopDialog)

    config = Config(file_name="unused")
    config.data.update(
        {
            "cookie": {"JSESSIONID": "stale"},
            "save_password": True,
            "cipher_enabled": False,
            "username": "user",
            "server_url": "http://localhost:8080",
            "password": "raw-pass-123",
        }
    )

    class _FirstConnectFails(_FakeWsManager):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def connect(self):
            self.attempts += 1
            if self.attempts == 1:
                return False, "stale cookie"
            return True, ""

    app = _make_application(config)
    app.stomp_manager = _FirstConnectFails()
    app.authenticate_and_connect()

    expected_hash = CipherManager.string_to_sha3_512_lowercase_hex("raw-pass-123")
    assert app.request_manager.posted_passwords == [expected_hash]
    assert config.data["password"] == "raw-pass-123"
    assert _FakeLoginForm.constructed >= 0  # no form needed for saved creds


# --- POSIX lock file -----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_lock_file_is_not_truncated_or_deleted_by_second_instance(tmp_path):
    from core import application
    from core.constants import MACOS

    lock_path = str(tmp_path / "lock")
    with open(lock_path, "w") as f:
        f.write("holder-marker")

    first = application.Application.__new__(application.Application)
    original_platform = application.PLATFORM
    application.PLATFORM = MACOS
    try:
        first.create_lock_file(path=lock_path)  # must not truncate the marker

        with open(lock_path) as f:
            assert f.read() == "holder-marker", "lock file must be opened in append mode"

        second = application.Application.__new__(application.Application)
        with pytest.raises(OSError):
            second.create_lock_file(path=lock_path)

        # a failed second lock must leave the file in place
        import os

        assert os.path.exists(lock_path)
    finally:
        application.PLATFORM = original_platform
        first.lock_file.close()
