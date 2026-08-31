"""T3: unit tests for core.device_metadata.

Covers identity generation/persistence (random UUID4, computer-name default,
never hardware-derived), outgoing metadata construction under the
share_device_name toggle, and defensive validation of received metadata
(valid, missing, malformed, oversized, control-character cases).
"""

import logging
import os
import uuid

import pytest

from core import device_metadata
from core.config import Config
from core.device_metadata import (
    DEVICE_NAME_MAX_CHARS,
    KNOWN_CLIENT_PLATFORMS,
    ensure_device_identity,
    extract_remote_identity,
    outgoing_device_metadata,
    sanitize_device_name,
)


def _valid_metadata(device_id=None, **overrides):
    entry = {
        "deviceId": device_id or "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
        "deviceName": "Office PC",
        "clientPlatform": "Windows",
        "historyProtocolVersion": 1,
    }
    entry.update(overrides)
    return entry


# --- identity generation & persistence ---------------------------------------


def test_ensure_device_identity_generates_and_persists_uuid4(tmp_path):
    config = Config(file_name=str(tmp_path / "DATA"))

    ensure_device_identity(config)
    config.save()

    assert config.data["device_id"]
    parsed = uuid.UUID(config.data["device_id"])
    assert parsed.version == 4  # random, never hardware/MAC-derived


def test_ensure_device_identity_survives_reload(tmp_path):
    file_name = str(tmp_path / "DATA")
    first = Config(file_name=file_name)
    ensure_device_identity(first)
    first.save()

    second = Config(file_name=file_name)
    assert second.load() is True
    assert second.data["device_id"] == first.data["device_id"]
    # And a subsequent ensure must not regenerate it.
    ensure_device_identity(second)
    assert second.data["device_id"] == first.data["device_id"]


def test_ensure_device_identity_defaults_name_to_computer_name(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        device_metadata.socket, "gethostname", lambda: "test-pc\x01 name "
    )
    config = Config(file_name=str(tmp_path / "DATA"))

    ensure_device_identity(config)

    assert config.data["device_name"] == "test-pc name"


def test_ensure_device_identity_keeps_existing_values(tmp_path):
    config = Config(file_name=str(tmp_path / "DATA"))
    config.data["device_id"] = "existing-id"
    config.data["device_name"] = "Custom Name"

    ensure_device_identity(config)

    assert config.data["device_id"] == "existing-id"
    assert config.data["device_name"] == "Custom Name"


def test_generated_device_id_is_not_a_hardware_identifier(tmp_path):
    """The UUID must be random: not derived from the hostname, not a MAC
    (UUID.getnode()-style) address, and fresh on every generation site."""
    config = Config(file_name=str(tmp_path / "DATA"))
    ensure_device_identity(config)

    device_uuid = uuid.UUID(config.data["device_id"])
    assert device_uuid.version == 4
    assert device_uuid.node != uuid.getnode()  # MAC-based node would match this
    assert config.data["device_id"] != device_metadata.default_device_name()


# --- sanitize_device_name ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Office PC", "Office PC"),
        ("  padded  ", "padded"),
        ("bad\x00\x1f\x7fname", "badname"),
        ("tab\tname", "tabname"),  # \t is category Cc → stripped
        ("line\nbreak", "linebreak"),
        ("", ""),
    ],
)
def test_sanitize_device_name_strips_controls_and_trims(raw, expected):
    assert sanitize_device_name(raw) == expected


def test_sanitize_device_name_caps_length():
    assert len(sanitize_device_name("x" * 500)) == DEVICE_NAME_MAX_CHARS


def test_sanitize_device_name_non_string_is_empty():
    assert sanitize_device_name(None) == ""
    assert sanitize_device_name(12345) == ""


# --- outgoing_device_metadata ---------------------------------------------------


def test_outgoing_metadata_full_shape_when_sharing_enabled(tmp_config):
    tmp_config.data["device_id"] = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    tmp_config.data["device_name"] = "Office PC"
    tmp_config.data["share_device_name"] = True

    entry = outgoing_device_metadata(tmp_config)

    assert entry["deviceId"] == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    assert entry["deviceName"] == "Office PC"
    assert entry["clientPlatform"] in KNOWN_CLIENT_PLATFORMS
    assert entry["historyProtocolVersion"] == 1
    assert type(entry["historyProtocolVersion"]) is int


def test_outgoing_metadata_omits_name_when_sharing_disabled(tmp_config):
    tmp_config.data["device_id"] = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    tmp_config.data["device_name"] = "Office PC"
    tmp_config.data["share_device_name"] = False

    entry = outgoing_device_metadata(tmp_config)

    assert "deviceName" not in entry
    assert entry["deviceId"] == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"


def test_outgoing_metadata_omits_empty_name(tmp_config):
    tmp_config.data["device_id"] = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    tmp_config.data["device_name"] = ""
    tmp_config.data["share_device_name"] = True

    assert "deviceName" not in outgoing_device_metadata(tmp_config)


def test_outgoing_metadata_is_none_without_identity(tmp_config):
    assert outgoing_device_metadata(tmp_config) is None


def test_outgoing_metadata_sanitizes_name_before_sending(tmp_config):
    tmp_config.data["device_id"] = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    tmp_config.data["device_name"] = "My\x00PC"
    tmp_config.data["share_device_name"] = True

    assert outgoing_device_metadata(tmp_config)["deviceName"] == "MyPC"


# --- extract_remote_identity ----------------------------------------------------


def test_extract_valid_metadata_returns_canonical_identity():
    device_id, device_name = extract_remote_identity(_valid_metadata())

    assert device_id == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    assert device_name == "Office PC"


def test_extract_uppercase_uuid_is_canonicalized():
    device_id, _ = extract_remote_identity(
        _valid_metadata(deviceId="6F9619FF-8B86-D011-B42D-00CF4FC964FF")
    )
    assert device_id == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "not-a-dict",
        12345,
        {},
        _valid_metadata(deviceId=None),
        _valid_metadata(deviceId=""),
        _valid_metadata(deviceId="not-a-uuid"),
        _valid_metadata(deviceId=12345),  # bad type
        _valid_metadata(deviceId=["6f9619ff-8b86-d011-b42d-00cf4fc964ff"]),
        _valid_metadata(clientPlatform=None),
        _valid_metadata(clientPlatform="Solaris"),  # not in the allow-list
        _valid_metadata(clientPlatform=12345),
        _valid_metadata(historyProtocolVersion=None),
        _valid_metadata(historyProtocolVersion="1"),  # string, not int
        _valid_metadata(historyProtocolVersion=True),  # bool is not a version
        _valid_metadata(historyProtocolVersion=2),
        _valid_metadata(historyProtocolVersion=1.0),  # JSON float
    ],
)
def test_extract_missing_or_malformed_metadata_maps_to_remote_device(metadata):
    assert extract_remote_identity(metadata) == (None, None)


def test_extract_missing_name_keeps_identity():
    metadata = _valid_metadata()
    del metadata["deviceName"]
    assert extract_remote_identity(metadata) == (
        "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
        None,
    )


def test_extract_name_over_64_chars_drops_name_keeps_identity():
    metadata = _valid_metadata(deviceName="x" * (DEVICE_NAME_MAX_CHARS + 1))
    device_id, device_name = extract_remote_identity(metadata)
    assert device_id == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    assert device_name is None


def test_extract_name_at_limit_is_accepted():
    metadata = _valid_metadata(deviceName="x" * DEVICE_NAME_MAX_CHARS)
    _, device_name = extract_remote_identity(metadata)
    assert len(device_name) == DEVICE_NAME_MAX_CHARS


def test_extract_name_with_control_characters_is_stripped():
    metadata = _valid_metadata(deviceName="Off\x00ice\x1f PC\x7f")
    _, device_name = extract_remote_identity(metadata)
    assert device_name == "Office PC"


def test_extract_name_empty_after_strip_drops_name_only():
    metadata = _valid_metadata(deviceName="\x00\x1f")
    device_id, device_name = extract_remote_identity(metadata)
    assert device_id is not None
    assert device_name is None


def test_extract_name_bad_type_drops_name_only():
    metadata = _valid_metadata(deviceName=12345)
    device_id, device_name = extract_remote_identity(metadata)
    assert device_id is not None
    assert device_name is None


def test_extract_unicode_name_is_preserved():
    metadata = _valid_metadata(deviceName="PC-ünïcode™")
    _, device_name = extract_remote_identity(metadata)
    assert device_name == "PC-ünïcode™"


# --- log hygiene -----------------------------------------------------------------


def test_no_device_name_reaches_any_log_record(tmp_config, caplog):
    """Log-scan gate: exercising every T3 code path with a canary device name
    must not write that name into any log record."""
    canary = "Zebra-Canary-HostName"
    tmp_config.data["device_id"] = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    tmp_config.data["device_name"] = canary
    tmp_config.data["share_device_name"] = True

    with caplog.at_level(logging.DEBUG):
        outgoing_device_metadata(tmp_config)
        extract_remote_identity(_valid_metadata(deviceName=canary))
        extract_remote_identity(_valid_metadata(deviceName="x" * 500))
        extract_remote_identity(_valid_metadata(deviceId="garbage"))
        sanitize_device_name(canary)
        ensure_device_identity(tmp_config)

    for record in caplog.records:
        assert canary not in record.getMessage()
        assert canary not in record.msg


def test_unknown_extra_metadata_keys_are_tolerated():
    metadata = _valid_metadata()
    metadata["someFutureField"] = {"nested": True}
    assert extract_remote_identity(metadata) == (
        "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
        "Office PC",
    )


def test_default_device_name_never_raises_when_hostname_unavailable(monkeypatch):
    def boom():
        raise OSError("no hostname available")

    monkeypatch.setattr(device_metadata.socket, "gethostname", boom)
    assert device_metadata.default_device_name() == ""


def test_ensure_device_identity_writes_device_file_only_once(tmp_path):
    file_name = str(tmp_path / "DATA")
    config = Config(file_name=file_name)
    ensure_device_identity(config)
    assert os.path.isfile(file_name)
