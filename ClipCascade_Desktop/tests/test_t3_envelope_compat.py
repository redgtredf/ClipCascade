"""T3: envelope tests and the P2S/P2P compatibility matrix.

Covers device metadata attach on send (P2S body, P2P fragment metadata),
defensive validation on receive (valid / missing / malformed / oversized /
control-char names), fragment-stream consistency (conflict rejects the whole
stream before history insertion), and updated<->legacy interoperability in
both directions for both transports.
"""

import asyncio
import json
import logging
import time

from clipboard.clipboard_manager import ClipboardManager
from core.config import Config
from core.device_metadata import outgoing_device_metadata
from core.fragment_utils import is_valid_fragment_metadata
from p2p.p2p_manager import P2PManager
from stomp_ws.stomp_manager import STOMPManager

DEVICE_ID = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
DEVICE_NAME = "Office PC"


def _device_metadata(**overrides):
    entry = {
        "deviceId": DEVICE_ID,
        "deviceName": DEVICE_NAME,
        "clientPlatform": "Windows",
        "historyProtocolVersion": 1,
    }
    entry.update(overrides)
    return entry


def _identity_config():
    cfg = Config(file_name="unused-in-t3-tests")
    cfg.data["cipher_enabled"] = False
    cfg.data["device_id"] = DEVICE_ID
    cfg.data["device_name"] = DEVICE_NAME
    cfg.data["share_device_name"] = True
    return cfg


class FakeHistorySink:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class FakeStompClient:
    def __init__(self):
        self.sent = []

    def send(self, destination, body):
        self.sent.append(body)


class FakeFrame:
    def __init__(self, body_dict):
        self.body = json.dumps(body_dict)


class FakeDataChannel:
    readyState = "open"

    def __init__(self, captured):
        self._captured = captured

    def send(self, body):
        self._captured.append(body)


def _make_stomp(sink, cfg):
    stomp = STOMPManager(cfg, history_sink=sink)
    stomp.is_connected = True
    stomp.client = FakeStompClient()
    return stomp


def _make_p2p(sink, cfg):
    p2p = P2PManager(cfg, history_sink=sink)
    return p2p


def _fragment_bodies(payload_chunks, device=None, stream_id="stream-1"):
    """Build a fragmented P2P stream the way the sender does."""
    bodies = []
    for index, chunk in enumerate(payload_chunks):
        metadata = {
            "id": stream_id,
            "isFragmented": len(payload_chunks) > 1,
            "index": index,
            "totalFragments": len(payload_chunks),
            "combinedRawPayloadSizeInBytes": 12345,
        }
        if device is not None:
            metadata["device"] = device
        bodies.append({"payload": chunk, "type": "text", "metadata": metadata})
    return bodies


# --- P2S send -----------------------------------------------------------------


def test_p2s_send_attaches_device_metadata():
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp.send("hello", "text")

    body = json.loads(stomp.client.sent[0])
    assert body["payload"] == "hello"
    assert body["type"] == "text"
    assert body["metadata"] == outgoing_device_metadata(cfg)
    assert body["metadata"]["deviceId"] == DEVICE_ID


def test_p2s_send_without_identity_stays_legacy_shaped():
    sink = FakeHistorySink()
    cfg = _identity_config()
    cfg.data["device_id"] = ""
    stomp = _make_stomp(sink, cfg)

    stomp.send("hello", "text")

    body = json.loads(stomp.client.sent[0])
    assert "metadata" not in body  # byte-shape identical to a legacy sender


def test_p2s_send_omits_name_when_sharing_disabled():
    sink = FakeHistorySink()
    cfg = _identity_config()
    cfg.data["share_device_name"] = False
    stomp = _make_stomp(sink, cfg)

    stomp.send("hello", "text")

    body = json.loads(stomp.client.sent[0])
    assert body["metadata"]["deviceId"] == DEVICE_ID
    assert "deviceName" not in body["metadata"]


# --- P2S receive ----------------------------------------------------------------


def test_p2s_receive_valid_metadata_feeds_history_sink():
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp._receive(
        FakeFrame({"payload": "remote text", "type": "text", "metadata": _device_metadata()})
    )

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "remote"
    assert event.transport == "p2s"
    assert event.payload == "remote text"
    assert event.source_device_id == DEVICE_ID
    assert event.source_device_name == DEVICE_NAME


def test_p2s_receive_missing_metadata_maps_to_remote_device():
    """Updated receiver + legacy sender: no metadata, payload still applied."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp._receive(FakeFrame({"payload": "legacy text", "type": "text"}))

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.source_device_id is None
    assert event.source_device_name is None
    assert event.payload == "legacy text"


def test_p2s_receive_malformed_metadata_maps_to_remote_device_but_keeps_content():
    for bad_metadata in (
        _device_metadata(deviceId="not-a-uuid"),
        _device_metadata(deviceId=12345),
        _device_metadata(clientPlatform="Solaris"),
        _device_metadata(historyProtocolVersion="1"),
        "metadata-as-a-string",
    ):
        sink = FakeHistorySink()
        cfg = _identity_config()
        stomp = _make_stomp(sink, cfg)

        stomp._receive(
            FakeFrame({"payload": "still valid", "type": "text", "metadata": bad_metadata})
        )

        # Content must never be rejected because of metadata problems.
        assert len(sink.events) == 1
        assert sink.events[0].payload == "still valid"
        assert sink.events[0].source_device_id is None
        assert sink.events[0].source_device_name is None


def test_p2s_receive_oversized_name_drops_name_keeps_identity():
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp._receive(
        FakeFrame(
            {
                "payload": "hello",
                "type": "text",
                "metadata": _device_metadata(deviceName="x" * 65),
            }
        )
    )

    assert sink.events[0].source_device_id == DEVICE_ID
    assert sink.events[0].source_device_name is None


def test_p2s_receive_name_with_control_characters_is_stripped():
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp._receive(
        FakeFrame(
            {
                "payload": "hello",
                "type": "text",
                "metadata": _device_metadata(deviceName="Off\x00ice\x7f PC"),
            }
        )
    )

    assert sink.events[0].source_device_name == "Office PC"


# --- P2P send -------------------------------------------------------------------


def test_p2p_send_attaches_identical_device_metadata_to_every_fragment():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        captured = []
        p2p.data_channels = {"peer-1": FakeDataChannel(captured)}

        asyncio.run(p2p._send("x" * 40000, "text"))  # spans 3 fragments

        assert len(captured) == 3
        devices = [json.loads(body)["metadata"]["device"] for body in captured]
        assert all(device == devices[0] for device in devices)
        assert devices[0]["deviceId"] == DEVICE_ID
        assert devices[0]["deviceName"] == DEVICE_NAME
        # Fragment fields stay untouched alongside the device metadata.
        indexes = [json.loads(body)["metadata"]["index"] for body in captured]
        assert indexes == [0, 1, 2]
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_send_single_frame_also_carries_device_metadata():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        captured = []
        p2p.data_channels = {"peer-1": FakeDataChannel(captured)}

        asyncio.run(p2p._send("tiny", "text"))

        assert len(captured) == 1
        metadata = json.loads(captured[0])["metadata"]
        assert metadata["isFragmented"] is False
        assert metadata["device"]["deviceId"] == DEVICE_ID
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_send_without_identity_stays_legacy_shaped():
    sink = FakeHistorySink()
    cfg = _identity_config()
    cfg.data["device_id"] = ""
    p2p = _make_p2p(sink, cfg)
    try:
        captured = []
        p2p.data_channels = {"peer-1": FakeDataChannel(captured)}

        asyncio.run(p2p._send("hello", "text"))

        metadata = json.loads(captured[0])["metadata"]
        assert "device" not in metadata  # identical to a legacy sender's envelope
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


# --- P2P receive ----------------------------------------------------------------


def test_p2p_receive_single_frame_valid_metadata_feeds_history_sink():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        body = {
            "payload": "remote text",
            "type": "text",
            "metadata": {"isFragmented": False, "device": _device_metadata()},
        }
        p2p._receive(json.dumps(body))

        assert len(sink.events) == 1
        assert sink.events[0].source_device_id == DEVICE_ID
        assert sink.events[0].source_device_name == DEVICE_NAME
        assert sink.events[0].transport == "p2p"
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_receive_fragmented_stream_uses_stream_device_metadata():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        for body in _fragment_bodies(["part1", "part2"], device=_device_metadata()):
            p2p._receive(json.dumps(body))

        assert len(sink.events) == 1
        assert sink.events[0].payload == "part1part2"
        assert sink.events[0].source_device_id == DEVICE_ID
        assert sink.events[0].source_device_name == DEVICE_NAME
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_receive_fragmented_stream_without_device_maps_to_remote_device():
    """Updated receiver + legacy P2P sender (fragment metadata, no device)."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        for body in _fragment_bodies(["part1", "part2"], device=None):
            p2p._receive(json.dumps(body))

        assert len(sink.events) == 1
        assert sink.events[0].payload == "part1part2"
        assert sink.events[0].source_device_id is None
        assert sink.events[0].source_device_name is None
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_conflicting_device_metadata_rejects_whole_stream():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        conflict = _device_metadata(deviceId="11111111-2222-3333-4444-555555555555")
        bodies = _fragment_bodies(["part1", "part2"], device=_device_metadata())
        bodies[1]["metadata"]["device"] = conflict

        for body in bodies:
            p2p._receive(json.dumps(body))

        # Nothing delivered, nothing recorded, receiver state cleared.
        assert sink.events == []
        assert p2p.receiving_fragments == {}
        assert p2p.receiving_fragment_devices == {}
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_conflict_rejection_happens_before_history_insertion():
    """Fragment 0 (device A) alone must not create history when fragment 1
    (device B) rejects the stream — the stream never reaches delivery."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        first = _fragment_bodies(["part1", "part2"], device=_device_metadata())[0]
        p2p._receive(json.dumps(first))
        assert sink.events == []  # partial stream is never recorded

        conflicting = {
            "payload": "part2",
            "type": "text",
            "metadata": {
                "id": first["metadata"]["id"],
                "isFragmented": True,
                "index": 1,
                "totalFragments": 2,
                "combinedRawPayloadSizeInBytes": 12345,
                "device": _device_metadata(deviceName="Impostor PC"),
            },
        }
        p2p._receive(json.dumps(conflicting))

        assert sink.events == []  # no history insertion from the rejected stream
        assert p2p.receiving_fragments == {}
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_conflicting_metadata_never_writes_device_name_to_logs(caplog):
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        bodies = _fragment_bodies(["part1", "part2"], device=_device_metadata())
        bodies[1]["metadata"]["device"] = _device_metadata(deviceName="Impostor PC")

        with caplog.at_level(logging.DEBUG):
            for body in bodies:
                p2p._receive(json.dumps(body))

        for record in caplog.records:
            assert "Impostor PC" not in record.getMessage()
            assert DEVICE_NAME not in record.getMessage()
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_later_fragment_without_device_keeps_stream_state():
    """A sender may repeat device metadata only in fragment zero; the stream
    state established there governs, and omission is not a conflict."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        bodies = _fragment_bodies(["part1", "part2"], device=_device_metadata())
        del bodies[1]["metadata"]["device"]

        for body in bodies:
            p2p._receive(json.dumps(body))

        assert len(sink.events) == 1
        assert sink.events[0].source_device_id == DEVICE_ID
        assert sink.events[0].source_device_name == DEVICE_NAME
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_p2p_device_appearing_midstream_after_absent_zero_rejects_stream():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        bodies = _fragment_bodies(["part1", "part2"], device=None)
        bodies[1]["metadata"]["device"] = _device_metadata()

        for body in bodies:
            p2p._receive(json.dumps(body))

        assert sink.events == []
        assert p2p.receiving_fragments == {}
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


# --- compatibility matrix ---------------------------------------------------------


def legacy_p2s_receive(clipboard_manager, body):
    """Verbatim pre-T3 stomp_manager._receive core (payload/type only)."""
    payload = body["payload"]
    payload_type = body.get("type", "text")
    if clipboard_manager.has_clipboard_changed(payload):
        clipboard_manager.base64_to_clipboard(
            base64_string=payload, type_=payload_type
        )


def legacy_p2p_reassemble(bodies):
    """Verbatim pre-T3 fragment reassembly core (fragment fields only)."""
    fragments = {}
    for body in bodies:
        metadata = body["metadata"]
        assert is_valid_fragment_metadata(
            metadata["totalFragments"], metadata["index"]
        )
        if metadata["id"] in fragments:
            fragments[metadata["id"]][metadata["index"]] = body["payload"]
        else:
            fragments[metadata["id"]] = [""] * metadata["totalFragments"]
            fragments[metadata["id"]][metadata["index"]] = body["payload"]
    (assembled,) = [
        "".join(parts) for parts in fragments.values() if all(parts)
    ]
    return assembled


def test_matrix_p2s_updated_sender_to_legacy_receiver():
    """Legacy receiver ignores the unknown metadata field and keeps working."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)
    stomp.send("compat text", "text")
    body = json.loads(stomp.client.sent[0])
    assert "metadata" in body  # updated sender

    legacy_sink = FakeHistorySink()
    legacy_manager = ClipboardManager(cfg, history_sink=legacy_sink, history_transport="p2s")
    legacy_p2s_receive(legacy_manager, body)  # must not raise

    assert legacy_sink.events[0].payload == "compat text"
    assert legacy_sink.events[0].source_device_id is None


def test_matrix_p2s_legacy_sender_to_updated_receiver():
    sink = FakeHistorySink()
    cfg = _identity_config()
    stomp = _make_stomp(sink, cfg)

    stomp._receive(FakeFrame({"payload": "legacy text", "type": "text"}))

    assert len(sink.events) == 1
    assert sink.events[0].payload == "legacy text"
    assert sink.events[0].source_device_id is None  # "Remote device"


def test_matrix_p2p_updated_sender_to_legacy_reassembly():
    """A legacy reassembler processes the updated fragment envelope: the
    device key is ignored and the payload reassembles byte-identically."""
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        captured = []
        p2p.data_channels = {"peer-1": FakeDataChannel(captured)}
        original = "compat payload " * 2000  # spans multiple fragments

        asyncio.run(p2p._send(original, "text"))

        assert len(captured) > 1
        assert legacy_p2p_reassemble([json.loads(body) for body in captured]) == original
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


def test_matrix_p2p_legacy_sender_to_updated_receiver():
    sink = FakeHistorySink()
    cfg = _identity_config()
    p2p = _make_p2p(sink, cfg)
    try:
        # Legacy P2P senders already send fragment metadata, never a device key.
        for body in _fragment_bodies(["part1", "part2"], device=None):
            p2p._receive(json.dumps(body))

        assert len(sink.events) == 1
        assert sink.events[0].payload == "part1part2"
        assert sink.events[0].source_device_id is None  # "Remote device"
    finally:
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)


# --- hot path --------------------------------------------------------------------


def test_metadata_attach_stays_cheap_on_the_send_hot_path():
    cfg = _identity_config()
    start = time.perf_counter()
    for _ in range(50000):
        outgoing_device_metadata(cfg)
    elapsed = time.perf_counter() - start
    # Dict build + string checks only, no I/O; generous bound for CI noise.
    assert elapsed < 2.0, f"outgoing_device_metadata became slow: {elapsed:.3f}s"
