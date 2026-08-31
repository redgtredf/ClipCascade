"""C-batch-2 hardening tests: Windows monitor format priority (text over
image on composite copies), atomic dedupe-hash compare-and-set with rollback
(no burned-hash missed captures), P2P offline send guard, and P2P delivery
off the asyncio loop.
"""

import json
import time

import pytest

from clipboard import clipboard_monitor_win as cm_win
from clipboard.clipboard_manager import ClipboardManager
from core.config import Config
from p2p.p2p_manager import P2PManager
from stomp_ws.stomp_manager import STOMPManager

CF_TEXT = 1
CF_BITMAP = 2
CF_UNICODETEXT = 13
CF_HDROP = 15


def _test_config(**overrides):
    cfg = Config(file_name="unused-in-hardening-tests")
    cfg.data["cipher_enabled"] = False
    for key, value in overrides.items():
        cfg.data[key] = value
    return cfg


# --- Windows monitor format priority -------------------------------------


class FakeClipboardFormats:
    def __init__(self, available, data):
        self.available = set(available)
        self.data = data

    def is_available(self, fmt):
        return fmt in self.available

    def get_data(self, fmt):
        return self.data[fmt]


def _patch_formats(monkeypatch, available, data=None, image=None):
    formats = FakeClipboardFormats(available, data or {})
    monkeypatch.setattr(cm_win.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cm_win.win32clipboard, "OpenClipboard", lambda: None)
    monkeypatch.setattr(cm_win.win32clipboard, "CloseClipboard", lambda: None)
    monkeypatch.setattr(
        cm_win.win32clipboard, "IsClipboardFormatAvailable", formats.is_available
    )
    monkeypatch.setattr(cm_win.win32clipboard, "GetClipboardData", formats.get_data)
    if image is not None:
        monkeypatch.setattr(cm_win.ImageGrab, "grabclipboard", lambda: image)


def test_composite_text_and_bitmap_prefers_text(monkeypatch):
    _patch_formats(
        monkeypatch,
        available=[CF_UNICODETEXT, CF_BITMAP],
        data={CF_UNICODETEXT: "copied words"},
        image=["should-not-be-used.png"],
    )
    content_type, content = cm_win._get_clipboard_content(
        enable_image_monitoring=True, enable_file_monitoring=True
    )
    assert (content_type, content) == ("text", "copied words")


def test_bitmap_only_still_reports_image(monkeypatch):
    image_sentinel = ["clipboard-image.png"]
    _patch_formats(monkeypatch, available=[CF_BITMAP], image=image_sentinel)
    content_type, content = cm_win._get_clipboard_content(enable_image_monitoring=True)
    assert content_type == "image"
    assert content == image_sentinel


def test_files_outrank_bitmap_when_no_text(monkeypatch):
    _patch_formats(
        monkeypatch,
        available=[CF_HDROP, CF_BITMAP],
        data={CF_HDROP: ("C:/a.txt", "C:/b.bin")},
        image=["should-not-be-used.png"],
    )
    content_type, content = cm_win._get_clipboard_content(
        enable_image_monitoring=True, enable_file_monitoring=True
    )
    assert content_type == "files"
    assert content == ("C:/a.txt", "C:/b.bin")


def test_no_known_format_returns_none(monkeypatch):
    _patch_formats(monkeypatch, available=[CF_BITMAP])
    content_type, content = cm_win._get_clipboard_content(enable_image_monitoring=False)
    assert (content_type, content) == (None, None)


def test_image_monitoring_disabled_ignores_bitmap(monkeypatch):
    _patch_formats(monkeypatch, available=[CF_BITMAP], image=["x.png"])
    content_type, _ = cm_win._get_clipboard_content(enable_image_monitoring=False)
    assert content_type is None


# --- dedupe hash atomicity + rollback -------------------------------------


def test_hash_rollback_restores_previous_value():
    manager = ClipboardManager(_test_config())
    assert manager.has_clipboard_changed("first") is True
    burned = manager.previous_clipboard_hash

    manager.restore_previous_clipboard_hash(0)
    assert manager.previous_clipboard_hash == 0
    # the same content now counts as changed again
    assert manager.has_clipboard_changed("first") is True
    assert manager.previous_clipboard_hash == burned

    manager.reset_previous_clipboard_hash()
    assert manager.previous_clipboard_hash == 0


def test_base64_to_clipboard_reports_failure(monkeypatch):
    manager = ClipboardManager(_test_config())

    def failing_paste(payload, payload_type):
        raise OSError("clipboard locked")

    monkeypatch.setattr(manager, "paste", failing_paste)
    assert manager.base64_to_clipboard("remote text", "text") is False

    pasted = []
    monkeypatch.setattr(manager, "paste", lambda p, t: pasted.append((p, t)))
    assert manager.base64_to_clipboard("remote text", "text") is True
    assert pasted == [("remote text", "text")]


# --- STOMP send/receive rollback ------------------------------------------


class FakeFrame:
    def __init__(self, body_dict):
        self.body = json.dumps(body_dict)


class SendingClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, destination, body):
        if self.fail:
            raise OSError("websocket closed mid-send")
        self.sent.append(body)


def _make_stomp(client):
    stomp = STOMPManager(_test_config(), is_login_phase=True)
    stomp.is_connected = True
    stomp.client = client
    return stomp


def test_stomp_send_failure_rolls_back_dedupe_hash():
    stomp = _make_stomp(SendingClient(fail=True))
    assert stomp.send("hello") is None
    assert stomp.clipboard_manager.previous_clipboard_hash == 0, (
        "a failed send must not burn the dedupe hash"
    )


def test_stomp_send_success_burns_dedupe_hash():
    client = SendingClient()
    stomp = _make_stomp(client)
    stomp.send("hello")
    assert client.sent, "payload must be sent"
    assert stomp.clipboard_manager.previous_clipboard_hash != 0


def test_stomp_receive_undelivered_payload_rolls_back(monkeypatch):
    stomp = _make_stomp(SendingClient())
    results = {"delivered": False}

    def fake_deliver(base64_string, type_, source_device_id, source_device_name):
        return results["delivered"]

    monkeypatch.setattr(
        stomp.clipboard_manager, "base64_to_clipboard", fake_deliver
    )
    stomp._receive(FakeFrame({"payload": "payload-a", "type": "text"}))
    assert stomp.clipboard_manager.previous_clipboard_hash == 0, (
        "an undelivered remote payload must not burn the dedupe hash"
    )

    results["delivered"] = True
    stomp._receive(FakeFrame({"payload": "payload-a", "type": "text"}))
    assert stomp.clipboard_manager.previous_clipboard_hash != 0


# --- P2P offline guard + delivery worker -----------------------------------


class FakeChannel:
    def __init__(self, fail=False):
        self.readyState = "open"
        self.fail = fail
        self.sent = []

    def send(self, body):
        if self.fail:
            raise OSError("data channel closed")
        self.sent.append(body)


def _make_p2p():
    return P2PManager(_test_config(), is_login_phase=True)


def _run_on_p2p_loop(p2p, coro, timeout=5.0):
    return p2p.schedule_task(coro).result(timeout)


def test_p2p_send_with_no_open_channels_does_not_burn_hash():
    p2p = _make_p2p()
    assert p2p.data_channels == {}
    _run_on_p2p_loop(p2p, p2p._send("offline payload", "text"))
    assert p2p.clipboard_manager.previous_clipboard_hash == 0, (
        "offline send must leave the dedupe hash un-burned for later delivery"
    )


def test_p2p_send_failure_rolls_back_hash():
    p2p = _make_p2p()
    p2p.data_channels["peer-1"] = FakeChannel(fail=True)
    _run_on_p2p_loop(p2p, p2p._send("hello", "text"))
    assert p2p.clipboard_manager.previous_clipboard_hash == 0


def test_p2p_send_success_burns_hash_and_fragments():
    p2p = _make_p2p()
    channel = FakeChannel()
    p2p.data_channels["peer-1"] = channel
    _run_on_p2p_loop(p2p, p2p._send("hello", "text"))
    assert channel.sent, "payload must reach the open channel"
    assert p2p.clipboard_manager.previous_clipboard_hash != 0


def test_p2p_receive_delivers_off_the_loop(monkeypatch):
    p2p = _make_p2p()
    delivered = []

    def fake_deliver(base64_string, type_, source_device_id, source_device_name):
        delivered.append((base64_string, type_))
        return True

    monkeypatch.setattr(p2p.clipboard_manager, "base64_to_clipboard", fake_deliver)

    p2p._receive(json.dumps({"payload": "p1", "type": "text"}))
    p2p._receive(json.dumps({"payload": "p2", "type": "text"}))

    deadline = time.monotonic() + 5
    while len(delivered) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert delivered == [("p1", "text"), ("p2", "text")], (
        "deliveries must happen (off the loop) and keep order"
    )


def test_p2p_receive_failed_paste_rolls_back(monkeypatch):
    p2p = _make_p2p()

    def fake_deliver(base64_string, type_, source_device_id, source_device_name):
        return False

    monkeypatch.setattr(p2p.clipboard_manager, "base64_to_clipboard", fake_deliver)

    p2p._receive(json.dumps({"payload": "p1", "type": "text"}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if p2p.clipboard_manager.previous_clipboard_hash != 0:
            pytest.fail("hash must stay un-burned while delivery keeps failing")
        time.sleep(0.05)
    # stayed 0 for the whole window: rollback path held
    assert p2p.clipboard_manager.previous_clipboard_hash == 0
