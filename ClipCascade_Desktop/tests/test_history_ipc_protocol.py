"""Pure framing tests for `history.ipc_protocol` -- no Windows, no pipes, no
sockets. `read_frame` is exercised against a fake `read_exact` so hostile
inputs (oversized/truncated/garbage) are cheap and deterministic to prove."""

import json

import pytest

from history import ipc_protocol


def _fixed_reader(data: bytes):
    """A read_exact(n) stand-in that serves bytes from a fixed buffer and
    raises ConnectionClosedError once it runs out -- like a closed pipe."""
    state = {"offset": 0}

    def read_exact(n):
        start = state["offset"]
        chunk = data[start : start + n]
        if len(chunk) != n:
            raise ipc_protocol.ConnectionClosedError("out of fixture bytes")
        state["offset"] += n
        return chunk

    return read_exact


def test_encode_then_read_frame_round_trips():
    payload = {"type": "request", "id": 1, "action": "ping"}
    frame = ipc_protocol.encode_frame(payload)
    decoded = ipc_protocol.read_frame(_fixed_reader(frame))
    assert decoded == payload


def test_encode_frame_rejects_oversized_outgoing_payload():
    huge = {"data": "x" * (ipc_protocol.MAX_FRAME_BYTES + 1)}
    with pytest.raises(ipc_protocol.FrameTooLargeError):
        ipc_protocol.encode_frame(huge)


def test_read_frame_rejects_oversized_declared_length_without_reading_body():
    """A hostile peer's declared length must be rejected before any attempt
    to read that many payload bytes -- proven by a reader that would raise
    if ever asked for more than a handful of bytes."""
    prefix = ipc_protocol._LENGTH_PREFIX.pack(ipc_protocol.MAX_FRAME_BYTES + 1)

    calls = []

    def read_exact(n):
        calls.append(n)
        if n > 64:
            raise AssertionError("read_frame must not read an oversized body")
        return prefix[: n]

    with pytest.raises(ipc_protocol.FrameTooLargeError):
        ipc_protocol.read_frame(read_exact)
    # Only the 4-byte length prefix was ever requested.
    assert calls == [ipc_protocol.LENGTH_PREFIX_SIZE]


def test_read_frame_rejects_malformed_json_body():
    body = b"not json at all"
    frame = ipc_protocol._LENGTH_PREFIX.pack(len(body)) + body
    with pytest.raises(ipc_protocol.MalformedFrameError):
        ipc_protocol.read_frame(_fixed_reader(frame))


def test_read_frame_rejects_non_object_json_body():
    body = json.dumps([1, 2, 3]).encode("utf-8")
    frame = ipc_protocol._LENGTH_PREFIX.pack(len(body)) + body
    with pytest.raises(ipc_protocol.MalformedFrameError):
        ipc_protocol.read_frame(_fixed_reader(frame))


def test_read_frame_rejects_truncated_prefix():
    with pytest.raises(ipc_protocol.ConnectionClosedError):
        ipc_protocol.read_frame(_fixed_reader(b"\x00\x00"))


def test_read_frame_rejects_truncated_body():
    body = b'{"type": "ping"}'
    # Declare more bytes than are actually supplied.
    frame = ipc_protocol._LENGTH_PREFIX.pack(len(body) + 10) + body
    with pytest.raises(ipc_protocol.ConnectionClosedError):
        ipc_protocol.read_frame(_fixed_reader(frame))


def test_read_frame_handles_empty_body():
    frame = ipc_protocol._LENGTH_PREFIX.pack(2) + b"{}"
    assert ipc_protocol.read_frame(_fixed_reader(frame)) == {}
