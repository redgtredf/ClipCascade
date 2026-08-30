"""Shared, dependency-free framing for the authenticated history IPC channel.

Both `history/ipc.py` (main-process server) and `history_ui/client.py` (child
client) import only this module for wire-format details -- never each
other's package -- so the child process can never reach SQLite/DPAPI merely
by handling a response; it only ever imports this module (stdlib only) plus
whatever transport primitives it uses to move bytes.

Frame format on the wire: a 4-byte big-endian length prefix followed by that
many UTF-8 JSON bytes. The length prefix is validated against
`MAX_FRAME_BYTES` *before* any read of the payload happens, so a hostile peer
declaring a huge length can never force either side to allocate more than the
4-byte prefix itself costs.
"""

import json
import struct

_LENGTH_PREFIX = struct.Struct(">I")
LENGTH_PREFIX_SIZE = _LENGTH_PREFIX.size

# Generous enough for a 1,000-entry query page (previews capped at 500 chars
# each) or a single decrypted image detail payload, small enough that a
# hostile peer can never force a large allocation before the frame is even
# validated -- read_frame() checks the declared length against this ceiling
# before reading a single payload byte.
MAX_FRAME_BYTES = 8 * 1024 * 1024

MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 50

PROTOCOL_VERSION = 1

TYPE_AUTH = "auth"
TYPE_AUTH_OK = "auth_ok"
TYPE_REQUEST = "request"
TYPE_RESPONSE = "response"
TYPE_EVENT = "event"
TYPE_CONTROL = "control"
TYPE_FOCUS_ACK = "focus_ack"

# Event names that mutate list/detail state and therefore carry a sequence
# number for gap detection. Control frames (focus_window, and any future
# heartbeat) are deliberately unsequenced so a client reconnect or a missed
# heartbeat never triggers a spurious full-page refresh.
DATA_EVENT_NAMES = (
    "entry_added",
    "entry_updated",
    "entry_deleted",
    "retention_changed",
    "service_disabled",
)


class FrameTooLargeError(RuntimeError):
    """A peer declared (or we tried to send) a frame above MAX_FRAME_BYTES."""


class MalformedFrameError(RuntimeError):
    """A frame's bytes did not decode to a JSON object."""


class ConnectionClosedError(RuntimeError):
    """The peer closed, reset or never had the connection while framing."""


def encode_frame(payload: dict) -> bytes:
    """Length-prefix one JSON object. Raises FrameTooLargeError rather than
    ever emitting a frame neither side could safely read back."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLargeError(
            f"outgoing frame is {len(body)} bytes (max {MAX_FRAME_BYTES})"
        )
    return _LENGTH_PREFIX.pack(len(body)) + body


def decode_frame_body(body: bytes) -> dict:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise MalformedFrameError(
            f"frame did not decode as UTF-8 JSON: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise MalformedFrameError("frame JSON was not an object")
    return decoded


def read_frame(read_exact) -> dict:
    """Read one frame using `read_exact(n) -> bytes`.

    `read_exact` must return exactly n bytes or raise ConnectionClosedError;
    transports (a Windows named pipe on either end, or a fake in a unit test)
    supply their own. The declared length is checked against MAX_FRAME_BYTES
    before any payload bytes are requested, so an oversized declaration never
    costs more than reading the 4-byte prefix.
    """
    prefix = read_exact(LENGTH_PREFIX_SIZE)
    if len(prefix) != LENGTH_PREFIX_SIZE:
        raise ConnectionClosedError("connection closed while reading a frame length")
    (length,) = _LENGTH_PREFIX.unpack(prefix)
    if length > MAX_FRAME_BYTES:
        raise FrameTooLargeError(
            f"peer declared a {length}-byte frame (max {MAX_FRAME_BYTES})"
        )
    body = read_exact(length) if length else b""
    if len(body) != length:
        raise ConnectionClosedError("connection closed mid-frame")
    return decode_frame_body(body)
