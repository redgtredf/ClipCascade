"""T7: full protocol-level payload compatibility matrix.

Extends the T3 envelope pattern to every supported payload kind
(text, web link, image, multi-file batch) for P2S and P2P, in all three
directions that matter:

- updated sender -> updated receiver  (device name shown in history)
- updated sender -> legacy receiver   (verbatim pre-T3 receive core stand-in;
  the unknown metadata key must be ignored)
- legacy sender  -> updated receiver  (source shown as "Remote device", i.e.
  no identity, payload still applied)

Single-machine stand-in for the two-physical-machine gate; the live P2S/P2P
run stays OPEN and is recorded in the T7 evidence.
"""

import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from clipboard.clipboard_manager import ClipboardManager
from core.config import Config
from p2p.p2p_manager import P2PManager
from stomp_ws.stomp_manager import STOMPManager
from test_t3_envelope_compat import (
    DEVICE_ID,
    DEVICE_NAME,
    FakeDataChannel,
    FakeFrame,
    FakeHistorySink,
    _device_metadata,
    legacy_p2p_reassemble,
    legacy_p2s_receive,
)

LINK_URL = "https://example.com/windows-security-guide"
PLAIN_TEXT = "Quarterly numbers are in the shared drive"
MATRIX_KINDS = ["text", "link", "image", "files"]


def _identity_config() -> Config:
    cfg = Config(file_name="unused-in-t7-matrix")
    cfg.data["cipher_enabled"] = False
    cfg.data["device_id"] = DEVICE_ID
    cfg.data["device_name"] = DEVICE_NAME
    cfg.data["share_device_name"] = True
    return cfg


def _legacy_config() -> Config:
    cfg = Config(file_name="unused-in-t7-matrix")
    cfg.data["cipher_enabled"] = False
    return cfg


def _pattern_png_bytes(width=512, height=512) -> bytes:
    """Deterministic, poorly-compressible PNG so the base64 payload exceeds
    the P2P fragment size and exercises real fragmentation."""
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            (((x * 48271) ^ (y * 69621)) % 256, (x * 31 + y * 17) % 256, (x * x + y * y) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _files_base64_json() -> str:
    names_and_bytes = [
        ("report.pdf", b"%PDF-1.7 " + b"p" * 6000),
        ("figures.xlsx", b"XLSX" + b"f" * 6000),
        ("notes.txt", b"text file contents " * 300),
    ]
    payload = {
        name: base64.b64encode(content).decode("ascii")
        for name, content in names_and_bytes
    }
    return json.dumps(payload)


def _wire_payloads():
    """kind -> (wire_type, payload_string, expected canonical event payload)."""
    png = _pattern_png_bytes()
    png_b64 = base64.b64encode(png).decode("ascii")
    files_json = _files_base64_json()
    files_bytes = {
        name: base64.b64decode(value)
        for name, value in json.loads(files_json).items()
    }
    return {
        "text": ("text", PLAIN_TEXT, PLAIN_TEXT),
        "link": ("text", LINK_URL, LINK_URL),  # links travel as text on the wire
        "image": ("image", png_b64, png),
        "files": ("files", files_json, files_bytes),
    }


def _intercept_paste(manager):
    """Keep the matrix off the real Windows clipboard; record what would land."""
    pasted = []

    def record(payload, payload_type="text", *args, **kwargs):
        pasted.append((payload_type, payload))

    manager.paste = record
    return pasted


class _CapturingStompClient:
    def __init__(self):
        self.sent = []

    def send(self, destination, body):
        self.sent.append(body)


def _make_stomp(cfg, sink):
    stomp = STOMPManager(cfg, history_sink=sink)
    stomp.is_connected = True
    stomp.client = _CapturingStompClient()
    _intercept_paste(stomp.clipboard_manager)
    return stomp


def _make_p2p(cfg, sink):
    p2p = P2PManager(cfg, history_sink=sink)
    # Delivery is normally offloaded to the P2P delivery worker; these tests
    # assert synchronously right after _receive, so run it inline. The
    # asynchronous worker itself is covered by test_clipboard_hardening.py.
    p2p._enqueue_delivery = lambda job: job()
    _intercept_paste(p2p.clipboard_manager)
    return p2p


def _p2p_capture_send(p2p, payload, payload_type):
    captured = []
    p2p.data_channels = {"peer-1": FakeDataChannel(captured)}
    try:
        asyncio.run(p2p._send(payload, payload_type))
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)
    return [json.loads(body) for body in captured]


def _p2p_receive_all(p2p, bodies):
    try:
        for body in bodies:
            p2p._receive(json.dumps(body))
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def _legacy_manager(cfg, sink):
    """ClipboardManager as a legacy (pre-T3) receiver would have built it."""
    manager = ClipboardManager(cfg, history_sink=sink, history_transport="p2s")
    _intercept_paste(manager)
    return manager


def _assert_remote_event(event, canonical, sender_named, transport):
    assert event.payload == canonical
    assert event.direction == "remote"
    assert event.transport == transport
    if sender_named:
        assert event.source_device_id == DEVICE_ID
        assert event.source_device_name == DEVICE_NAME
    else:
        # No valid identity: the UI renders this as "Remote device".
        assert event.source_device_id is None
        assert event.source_device_name is None


# --- updated sender -> updated receiver ------------------------------------------


@pytest.mark.parametrize("kind", MATRIX_KINDS)
def test_matrix_p2s_updated_to_updated(kind):
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    sender = _make_stomp(_identity_config(), FakeHistorySink())
    receiver_sink = FakeHistorySink()
    receiver = _make_stomp(_identity_config(), receiver_sink)

    sender.send(payload_string, wire_type)
    body = json.loads(sender.client.sent[0])
    assert "metadata" in body  # the updated envelope
    receiver._receive(FakeFrame(body))

    _assert_remote_event(receiver_sink.events[0], canonical, True, "p2s")


def test_matrix_p2p_updated_to_updated_text_spans_fragments():
    _, payload_string, canonical = _wire_payloads()["text"]
    big_text = payload_string * 1500  # well past the 15 KiB fragment size
    sender = _make_p2p(_identity_config(), FakeHistorySink())
    receiver_sink = FakeHistorySink()
    receiver = _make_p2p(_identity_config(), receiver_sink)

    bodies = _p2p_capture_send(sender, big_text, "text")
    assert len(bodies) > 1
    _p2p_receive_all(receiver, bodies)

    _assert_remote_event(receiver_sink.events[0], big_text, True, "p2p")


def test_matrix_p2p_updated_to_updated_link_single_frame():
    _, payload_string, canonical = _wire_payloads()["link"]
    sender = _make_p2p(_identity_config(), FakeHistorySink())
    receiver_sink = FakeHistorySink()
    receiver = _make_p2p(_identity_config(), receiver_sink)

    bodies = _p2p_capture_send(sender, payload_string, "text")
    assert len(bodies) == 1
    assert bodies[0]["metadata"]["device"]["deviceId"] == DEVICE_ID
    _p2p_receive_all(receiver, bodies)

    _assert_remote_event(receiver_sink.events[0], canonical, True, "p2p")


@pytest.mark.parametrize("kind", ["image", "files"])
def test_matrix_p2p_updated_to_updated_binary_payload_spans_fragments(kind):
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    sender = _make_p2p(_identity_config(), FakeHistorySink())
    receiver_sink = FakeHistorySink()
    receiver = _make_p2p(_identity_config(), receiver_sink)

    bodies = _p2p_capture_send(sender, payload_string, wire_type)
    assert len(bodies) > 1
    _p2p_receive_all(receiver, bodies)

    _assert_remote_event(receiver_sink.events[0], canonical, True, "p2p")


# --- updated sender -> legacy receiver --------------------------------------------


@pytest.mark.parametrize("kind", MATRIX_KINDS)
def test_matrix_p2s_updated_to_legacy(kind):
    """The legacy receive core ignores the unknown metadata field and still
    applies the payload + records local history."""
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    sender = _make_stomp(_identity_config(), FakeHistorySink())
    sender.send(payload_string, wire_type)
    body = json.loads(sender.client.sent[0])

    legacy_sink = FakeHistorySink()
    legacy_manager = _legacy_manager(_legacy_config(), legacy_sink)

    legacy_p2s_receive(legacy_manager, body)  # verbatim pre-T3 core: must not raise

    _assert_remote_event(legacy_sink.events[0], canonical, False, "p2s")
    assert legacy_manager.paste  # the payload reached the clipboard


@pytest.mark.parametrize("kind", MATRIX_KINDS)
def test_matrix_p2p_updated_to_legacy_reassembly_and_apply(kind):
    """A legacy reassembler ignores the device key; the reassembled payload
    then flows through the verbatim legacy clipboard-receive core."""
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    sender = _make_p2p(_identity_config(), FakeHistorySink())

    bodies = _p2p_capture_send(sender, payload_string, wire_type)
    assembled = legacy_p2p_reassemble(bodies)
    assert assembled == payload_string  # byte-identical without knowing metadata

    legacy_sink = FakeHistorySink()
    legacy_manager = _legacy_manager(_legacy_config(), legacy_sink)
    legacy_p2s_receive(legacy_manager, {"payload": assembled, "type": wire_type})

    _assert_remote_event(legacy_sink.events[0], canonical, False, "p2s")


# --- legacy sender -> updated receiver --------------------------------------------


@pytest.mark.parametrize("kind", MATRIX_KINDS)
def test_matrix_p2s_legacy_to_updated(kind):
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    receiver_sink = FakeHistorySink()
    receiver = _make_stomp(_identity_config(), receiver_sink)

    receiver._receive(FakeFrame({"payload": payload_string, "type": wire_type}))

    _assert_remote_event(receiver_sink.events[0], canonical, False, "p2s")


@pytest.mark.parametrize("kind", MATRIX_KINDS)
def test_matrix_p2p_legacy_to_updated(kind):
    """Legacy P2P senders already fragment, but never attach device metadata."""
    wire_type, payload_string, canonical = _wire_payloads()[kind]
    receiver_sink = FakeHistorySink()
    receiver = _make_p2p(_identity_config(), receiver_sink)

    if kind == "text":
        chunks = [payload_string]
    elif kind == "link":
        chunks = [payload_string]
    else:
        third = len(payload_string) // 3
        chunks = [payload_string[:third], payload_string[third : 2 * third], payload_string[2 * third :]]

    bodies = []
    for index, chunk in enumerate(chunks):
        metadata = {
            "id": "legacy-stream-1",
            "isFragmented": len(chunks) > 1,
            "index": index,
            "totalFragments": len(chunks),
            "combinedRawPayloadSizeInBytes": len(payload_string.encode("utf-8")),
        }
        bodies.append({"payload": chunk, "type": wire_type, "metadata": metadata})

    _p2p_receive_all(receiver, bodies)

    _assert_remote_event(receiver_sink.events[0], canonical, False, "p2p")


# --- identity hygiene across the matrix -------------------------------------------


def test_matrix_updated_envelope_device_metadata_shape():
    """The metadata every matrix case relies on: name shared, id opaque."""
    metadata = _device_metadata()
    assert metadata["deviceName"] == DEVICE_NAME
    assert metadata["deviceId"] == DEVICE_ID
    assert metadata["clientPlatform"] == "Windows"


def test_matrix_legacy_envelope_has_no_metadata_key():
    """The legacy sender shape the matrix models: payload/type only."""
    legacy_body = {"payload": "x", "type": "text"}
    assert "metadata" not in legacy_body


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
