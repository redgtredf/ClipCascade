"""Optional device metadata for P2S/P2P clipboard envelopes (T3).

Identity model (never hardware-derived):
- `device_id` is a random UUID4 generated once per install and persisted in
  the config file; it travels as an opaque, stable pseudonym.
- `device_name` defaults to the OS computer name, is user-editable, and is
  attached to outgoing messages only while `share_device_name` is enabled.
- Received metadata is validated defensively: anything missing or malformed
  degrades to "Remote device" (no source identity) and must never reject
  otherwise-valid clipboard content.

Device names must never be written to log records; the validation helpers
here report failures without including any metadata values.
"""

import socket
import unicodedata
import uuid

from core.constants import LINUX, MACOS, PLATFORM, WINDOWS

DEVICE_NAME_MAX_CHARS = 64
HISTORY_PROTOCOL_VERSION = 1

# Client platforms accepted in received metadata. Internal Linux
# display-server variants (Linux_X11/Wayland/Headless) normalize to "Linux"
# on send.
KNOWN_CLIENT_PLATFORMS = ("Windows", "macOS", "Linux", "Android")


def sanitize_device_name(raw) -> str:
    """Strip control characters, trim and cap a local device name."""
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc")
    return cleaned.strip()[:DEVICE_NAME_MAX_CHARS]


def default_device_name() -> str:
    """The OS computer name as the DEFAULT display name (editable, and only
    shared when the user enables it). Never used as an identifier."""
    try:
        return sanitize_device_name(socket.gethostname())
    except Exception:
        return ""


def ensure_device_identity(config) -> None:
    """Generate and persist the random device UUID and default name once.

    Called at startup after the config load. Saves immediately so the
    identity stays stable even if the process dies before the next regular
    config save.
    """
    changed = False
    if not isinstance(config.data.get("device_id"), str) or not config.data.get(
        "device_id"
    ):
        config.data["device_id"] = str(uuid.uuid4())
        changed = True
    if not str(config.data.get("device_name") or "").strip():
        config.data["device_name"] = default_device_name()
        changed = True
    if changed:
        config.save()


def wire_platform() -> str:
    if PLATFORM == MACOS:
        return "macOS"
    if PLATFORM.startswith(LINUX):
        return "Linux"
    if PLATFORM == WINDOWS:
        return "Windows"
    return "Unknown"


def outgoing_device_metadata(config):
    """Metadata dict for outgoing envelopes, or None when no identity exists
    (envelope stays legacy-shaped). The opaque device ID is always included
    once generated; the name only when sharing is enabled and non-empty."""
    device_id = config.data.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        return None
    entry = {
        "deviceId": device_id,
        "clientPlatform": wire_platform(),
        "historyProtocolVersion": HISTORY_PROTOCOL_VERSION,
    }
    if config.data.get("share_device_name"):
        name = sanitize_device_name(config.data.get("device_name") or "")
        if name:
            entry["deviceName"] = name
    return entry


def canonical_device_id(value):
    """Canonical lowercase-hyphenated form of a UUID string, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def extract_remote_identity(metadata):
    """Validate received device metadata into (device_id, device_name).

    Returns (None, None) — displayed as "Remote device" — whenever the
    metadata is absent or structurally invalid (bad UUID shape, unknown
    platform, wrong protocol version). A present-but-invalid device name
    only drops the name; the identity survives. Never raises and never
    logs metadata values.
    """
    if not isinstance(metadata, dict):
        return None, None

    device_id = canonical_device_id(metadata.get("deviceId"))
    if device_id is None:
        return None, None

    if metadata.get("clientPlatform") not in KNOWN_CLIENT_PLATFORMS:
        return None, None
    version = metadata.get("historyProtocolVersion")
    if type(version) is not int or version != HISTORY_PROTOCOL_VERSION:
        return None, None

    raw_name = metadata.get("deviceName")
    if not isinstance(raw_name, str):
        return device_id, None
    name = "".join(ch for ch in raw_name if unicodedata.category(ch) != "Cc").strip()
    if not 1 <= len(name) <= DEVICE_NAME_MAX_CHARS:
        return device_id, None
    return device_id, name
