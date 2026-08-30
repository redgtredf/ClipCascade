"""Live Windows named-pipe tests for the authenticated history IPC channel:
`history.ipc.HistoryIpcServer` (main process) talking to a real
`history_ui.client.HistoryIpcClient` (child) over a real OS pipe -- nothing
here is mocked. Covers auth accept/reject, hostile/oversized/malformed
frames, query/detail/command round trips, event publishing with sequence
numbers, focus ack (used to detect an unresponsive child), and a live
Windows ACL inspection of the pipe itself.
"""

import struct
import sys
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history IPC is Windows-only")

from history import ipc, ipc_protocol, models, service  # noqa: E402
from history_ui.client import HistoryIpcClient  # noqa: E402

import pywintypes  # noqa: E402
import win32file  # noqa: E402
import win32security  # noqa: E402


def _event(payload_type, payload, direction="local", transport="local"):
    return models.HistoryCaptureEvent(
        direction=direction,
        payload_type=payload_type,
        payload=payload,
        source_device_id=None,
        source_device_name=None,
        transport=transport,
        occurred_at_utc=datetime.now(timezone.utc),
    )


@pytest.fixture
def history_service_instance(tmp_path):
    result = service.bootstrap(str(tmp_path / "history"))
    assert result.enabled, result.disabled_reason
    yield result.service
    result.service.close()


@pytest.fixture
def server(history_service_instance):
    srv = ipc.HistoryIpcServer(history_service_instance)
    srv.start()
    yield srv
    srv.stop()


def _connected_client(server, timeout_s=3.0, **kwargs):
    pipe_name, token_hex = server.begin_session()
    client = HistoryIpcClient(pipe_name, bytes.fromhex(token_hex), **kwargs)
    assert client.connect(timeout_s=timeout_s) is True
    return client


# --- authentication ------------------------------------------------------


def test_auth_success_then_ping(server):
    client = _connected_client(server)
    try:
        assert client.ping() is True
    finally:
        client.close()


def test_wrong_token_receives_no_data_and_server_survives(server):
    pipe_name, _correct_token_hex = server.begin_session()
    wrong_client = HistoryIpcClient(pipe_name, b"\x00" * 32)
    assert wrong_client.connect(timeout_s=3.0) is False

    # The server must still work for a legitimate follow-up connection.
    good = _connected_client(server)
    try:
        assert good.ping() is True
    finally:
        good.close()


def test_missing_token_frame_is_rejected(server):
    pipe_name, _token_hex = server.begin_session()
    handle = _raw_connect(pipe_name)
    try:
        # First frame is not even an auth envelope.
        _raw_write(handle, ipc_protocol.encode_frame({"type": "request", "action": "ping"}))
        assert _raw_read_or_closed(handle) is None
    finally:
        win32file.CloseHandle(handle)

    good = _connected_client(server)
    try:
        assert good.ping() is True
    finally:
        good.close()


# --- hostile / oversized / malformed frames -------------------------------


def test_oversized_declared_frame_length_is_rejected_without_crashing(server):
    pipe_name, token_hex = server.begin_session()
    handle = _raw_connect(pipe_name)
    try:
        _raw_write(handle, ipc_protocol.encode_frame({"type": "auth", "token": token_hex}))
        auth_response = ipc_protocol.read_frame(lambda n: _raw_read_exact(handle, n))
        assert auth_response["type"] == "auth_ok"

        # Hand-craft a frame declaring a length far beyond MAX_FRAME_BYTES.
        oversized_prefix = struct.pack(">I", ipc_protocol.MAX_FRAME_BYTES + 1000)
        _raw_write(handle, oversized_prefix)
        assert _raw_read_or_closed(handle) is None
    finally:
        win32file.CloseHandle(handle)

    # Server thread must still be alive and serving new connections.
    good = _connected_client(server)
    try:
        assert good.ping() is True
    finally:
        good.close()


def test_malformed_json_body_is_rejected_without_crashing(server):
    pipe_name, token_hex = server.begin_session()
    handle = _raw_connect(pipe_name)
    try:
        _raw_write(handle, ipc_protocol.encode_frame({"type": "auth", "token": token_hex}))
        auth_response = ipc_protocol.read_frame(lambda n: _raw_read_exact(handle, n))
        assert auth_response["type"] == "auth_ok"

        garbage = b"not json"
        _raw_write(handle, struct.pack(">I", len(garbage)) + garbage)
        assert _raw_read_or_closed(handle) is None
    finally:
        win32file.CloseHandle(handle)

    good = _connected_client(server)
    try:
        assert good.ping() is True
    finally:
        good.close()


def test_malformed_request_after_auth_gets_an_error_not_a_crash(server):
    """Application-level bad input (unknown action) must error, not kill the
    connection -- only frame-level corruption does that."""
    client = _connected_client(server)
    try:
        with pytest.raises(Exception):
            client.execute_command("not-a-real-command")
        assert client.ping() is True
    finally:
        client.close()


# --- query / detail / command round trips ---------------------------------


def test_query_and_get_detail_round_trip(server, history_service_instance):
    entry_id = history_service_instance.record(_event("text", "hello over IPC"))
    client = _connected_client(server)
    try:
        page = client.query(page_size=10)
        assert page["entries"]
        assert any(e["id"] == entry_id for e in page["entries"])

        detail = client.get_detail(entry_id)
        assert detail["text"] == "hello over IPC"
        assert detail["image_base64"] is None
    finally:
        client.close()


def test_image_detail_round_trips_as_base64(server, history_service_instance):
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    entry_id = history_service_instance.record(_event("image", image_bytes))

    client = _connected_client(server)
    try:
        detail = client.get_detail(entry_id)
        assert base64.b64decode(detail["image_base64"]) == image_bytes
    finally:
        client.close()


def test_files_detail_never_carries_raw_file_bytes(server, history_service_instance):
    entry_id = history_service_instance.record(
        _event("files", {"report.pdf": b"CANARY_FILE_BYTES"})
    )
    client = _connected_client(server)
    try:
        detail = client.get_detail(entry_id)
        assert detail["files"] == [{"name": "report.pdf", "size_bytes": 17}]
        # No raw bytes field exists on a files entry's detail at all.
        assert "file_bytes" not in detail
        assert b"CANARY_FILE_BYTES" not in str(detail).encode("utf-8")
    finally:
        client.close()


def test_unknown_entry_id_reports_not_found(server):
    client = _connected_client(server)
    try:
        with pytest.raises(Exception):
            client.get_detail("does-not-exist")
    finally:
        client.close()


def test_pin_unpin_delete_round_trip_and_push_events(server, history_service_instance):
    entry_id = history_service_instance.record(_event("text", "pin me"))
    events = []
    client = _connected_client(
        server, on_event=lambda name, data, gap: events.append((name, data, gap))
    )
    try:
        result = client.execute_command("pin_entry", entry_id=entry_id)
        assert result["ok"] is True

        result = client.execute_command("unpin_entry", entry_id=entry_id)
        assert result["ok"] is True

        result = client.execute_command("delete_entry", entry_id=entry_id)
        assert result["ok"] is True
        assert result["affected_count"] == 1

        _wait_until(lambda: len(events) >= 3, timeout_s=3.0)
    finally:
        client.close()

    names = [name for name, _data, _gap in events]
    assert names == ["entry_updated", "entry_updated", "entry_deleted"]
    seqs = [data for _n, data, _g in events]
    assert seqs  # sanity: data payloads captured


def test_event_sequence_numbers_are_monotonic(server, history_service_instance):
    entry_id = history_service_instance.record(_event("text", "seq test"))
    client = _connected_client(server)

    raw_seqs = []
    original_handle_frame = client._handle_frame

    def patched_handle_frame(frame):
        if frame.get("type") == ipc_protocol.TYPE_EVENT:
            raw_seqs.append(frame.get("seq"))
        original_handle_frame(frame)

    client._handle_frame = patched_handle_frame

    try:
        client.execute_command("pin_entry", entry_id=entry_id)
        client.execute_command("unpin_entry", entry_id=entry_id)
        client.execute_command("delete_entry", entry_id=entry_id)
        _wait_until(lambda: len(raw_seqs) >= 3, timeout_s=3.0)
    finally:
        client.close()

    assert raw_seqs == sorted(raw_seqs)
    assert raw_seqs == list(range(raw_seqs[0], raw_seqs[0] + len(raw_seqs)))


def test_entry_added_event_fires_from_the_capture_path(server, history_service_instance):
    """`QueuedHistorySink`'s `on_recorded` hook is how the capture path (not
    a command) notifies IPC clients of new entries."""
    events = []
    client = _connected_client(
        server, on_event=lambda name, data, gap: events.append((name, data))
    )
    sink = service.QueuedHistorySink(
        history_service_instance, on_recorded=server.notify_entry_added
    )
    try:
        sink.record(_event("text", "captured live"))
        _wait_until(lambda: len(events) >= 1, timeout_s=3.0)
    finally:
        sink.stop()
        client.close()

    assert events[0][0] == "entry_added"


def test_retention_commands_emit_retention_changed(server, history_service_instance):
    history_service_instance.record(_event("text", "a"))
    history_service_instance.record(_event("text", "b"))
    events = []
    client = _connected_client(
        server, on_event=lambda name, data, gap: events.append(name)
    )
    try:
        result = client.execute_command("clear_all")
        assert result["ok"] is True
        _wait_until(lambda: "retention_changed" in events, timeout_s=3.0)
    finally:
        client.close()


def test_set_recording_enabled_emits_service_disabled(server):
    events = []
    client = _connected_client(
        server, on_event=lambda name, data, gap: events.append((name, data))
    )
    try:
        client.execute_command("set_recording_enabled", enabled=False)
        _wait_until(lambda: len(events) >= 1, timeout_s=3.0)
    finally:
        client.close()
    assert events[0] == ("service_disabled", {"disabled": True})


# --- focus ack (unresponsive-child detection) -----------------------------


def test_focus_request_succeeds_when_child_acks(server):
    client = _connected_client(server, on_focus=lambda: None)
    try:
        assert server.request_focus(timeout_s=2.0) is True
    finally:
        client.close()


def test_focus_request_times_out_when_child_never_acks(server):
    # No handling of control frames at all: the raw connection just never
    # answers, simulating a hung child whose reader loop is stuck.
    pipe_name, token_hex = server.begin_session()
    handle = _raw_connect(pipe_name)
    try:
        _raw_write(handle, ipc_protocol.encode_frame({"type": "auth", "token": token_hex}))
        auth_response = ipc_protocol.read_frame(lambda n: _raw_read_exact(handle, n))
        assert auth_response["type"] == "auth_ok"

        started = time.monotonic()
        assert server.request_focus(timeout_s=0.4) is False
        assert time.monotonic() - started < 2.0
    finally:
        win32file.CloseHandle(handle)


# --- ACL ------------------------------------------------------------------


def test_pipe_acl_is_restricted_to_current_user_and_system(server):
    import win32api

    # GetNamedSecurityInfo-by-path opens its own transient instance to read
    # the descriptor, so it must NOT be queried while a client holds the
    # (nMaxInstances=1) pipe's only instance -- that fails with "all pipe
    # instances are busy", not the ACL question this test asks. Instead,
    # just bound-retry past the startup race where the accept thread has
    # not yet reached CreateNamedPipe (ERROR_FILE_NOT_FOUND).
    security_descriptor = _retry_get_named_security_info(server._pipe_path)
    dacl = security_descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    current_user_sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    everyone_sid = win32security.ConvertStringSidToSid("S-1-1-0")
    authenticated_users_sid = win32security.ConvertStringSidToSid("S-1-5-11")

    granted_sids = []
    for i in range(dacl.GetAceCount()):
        _ace_type_and_flags, _mask, sid = dacl.GetAce(i)
        granted_sids.append(sid)

    assert any(current_user_sid == sid for sid in granted_sids)
    assert any(system_sid == sid for sid in granted_sids)
    assert not any(everyone_sid == sid for sid in granted_sids)
    assert not any(authenticated_users_sid == sid for sid in granted_sids)


# --- 1,000-entry paged-response memory ceiling ----------------------------


def test_1000_entry_page_stays_under_the_frame_ceiling(server, history_service_instance):
    for i in range(1000):
        history_service_instance.record(
            _event("text", f"entry number {i} " + ("x" * 400))
        )

    client = _connected_client(server, timeout_s=5.0)
    try:
        page = client.query(page_size=1000, timeout_s=15.0)
    finally:
        client.close()

    assert len(page["entries"]) == 1000
    import json

    encoded_bytes = len(json.dumps(page).encode("utf-8"))
    assert encoded_bytes < ipc_protocol.MAX_FRAME_BYTES
    # Evidence of the actual measured size, not just a pass/fail boundary.
    print(f"\n1000-entry page measured at {encoded_bytes} bytes "
          f"(ceiling {ipc_protocol.MAX_FRAME_BYTES} bytes)")


# --- raw (unauthenticated framing) helpers used by hostile-client tests ----


def _retry_get_named_security_info(pipe_path, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return win32security.GetNamedSecurityInfo(
                pipe_path, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
            )
        except pywintypes.error as error:
            if error.winerror != 2 or time.monotonic() >= deadline:  # ERROR_FILE_NOT_FOUND
                raise
            time.sleep(0.05)


def _raw_connect(pipe_name, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    pipe_path = r"\\.\pipe\%s" % pipe_name
    while True:
        try:
            handle = win32file.CreateFile(
                pipe_path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            return handle
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _raw_write(handle, data):
    win32file.WriteFile(handle, data)


def _raw_read_exact(handle, n):
    if n == 0:
        return b""
    chunks = []
    remaining = n
    while remaining > 0:
        try:
            _hr, data = win32file.ReadFile(handle, remaining)
        except pywintypes.error as error:
            # The server closing/disconnecting a rejected connection surfaces
            # here as a raw pywin32 error on a synchronous handle (e.g. 233
            # "No process is on the other end of the pipe"), not an empty
            # read -- translate it the same way the real client does.
            raise ipc_protocol.ConnectionClosedError(str(error)) from error
        if not data:
            raise ipc_protocol.ConnectionClosedError("closed")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _raw_read_or_closed(handle):
    """Returns None if the server closed the pipe (expected on rejection),
    or the decoded frame if it somehow still answered."""
    try:
        return ipc_protocol.read_frame(lambda n: _raw_read_exact(handle, n))
    except ipc_protocol.ConnectionClosedError:
        return None


def _wait_until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition never became true within timeout"
