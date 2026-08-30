"""Authenticated local IPC server: the main ClipCascade process's side of the
history child-process boundary.

Owns the Windows named pipe (ACL-restricted to the current user and SYSTEM),
the per-launch session token, and the request/response/event protocol
described in the technical plan. The child process (`history_ui/client.py`)
never receives the DPAPI/AES master key or a SQLite handle -- it only ever
sees whatever `HistoryService` returns already decrypted, shaped into JSON by
the handlers below. This module never imports Qt.

Topology: the main process is always the pipe *server*; the child always
*connects out* to it. That means a "please focus yourself" instruction is
just another server->client push over the same already-authenticated
connection (a `control` frame carrying an ack id), not a second channel --
`request_focus()` pushes it and blocks (bounded by `timeout_s`) for the
child's ack, which is also how an unresponsive (hung) child is detected.

The pipe is opened with `FILE_FLAG_OVERLAPPED` and every read/write uses a
fresh `OVERLAPPED` + event, even though each individual call still blocks
its own caller until done. This is not an optimization -- it is required
for correctness: a synchronous (non-overlapped) named pipe handle serializes
*all* I/O through the single handle, so the accept thread blocked reading
the next request and a different thread pushing an event (`request_focus`/
`notify_*`, called from the launcher or capture threads) would deadlock each
other. Overlapped I/O allows genuinely concurrent in-flight operations on
one handle, which is exactly what a push-capable duplex channel needs.
"""

import dataclasses
import hmac
import logging
import os
import secrets
import threading
import time
import uuid

from history import crypto, ipc_protocol, models

WINDOWS = os.name == "nt"

try:
    import pywintypes
    import win32event
    import win32file
    import win32pipe
    import win32security
    import winerror
    import ntsecuritycon as con
except ImportError:  # pragma: no cover - exercised only off Windows
    pywintypes = None
    win32event = None
    win32file = None
    win32pipe = None
    win32security = None
    winerror = None
    con = None


PIPE_NAME_PREFIX = "ClipCascade.HistoryIpc"
TOKEN_BYTES = 32
PIPE_BUFFER_SIZE = 65536

_ERROR_PIPE_CONNECTED = 535


class HistoryIpcUnavailableError(RuntimeError):
    """Raised when the authenticated IPC channel cannot run on this platform."""


class _RequestError(Exception):
    """Carries a stable, non-sensitive error code back to the client."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require_windows():
    if not WINDOWS or win32pipe is None:
        raise HistoryIpcUnavailableError(
            "The history IPC channel requires Windows named pipes"
        )


def new_pipe_name() -> str:
    return f"{PIPE_NAME_PREFIX}.{uuid.uuid4().hex}"


def new_session_token() -> bytes:
    return secrets.token_bytes(TOKEN_BYTES)


def _build_pipe_security_attributes():
    """Restrict the pipe to the current Windows user and SYSTEM only, with a
    protected DACL so no inherited ACE from a parent object can widen it --
    the same defense-in-depth pattern `history.crypto.secure_directory` uses
    for the on-disk store."""
    user_sid = crypto.current_user_sid()
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    dacl = win32security.ACL()
    for sid in (user_sid, system_sid):
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, con.FILE_ALL_ACCESS, sid)
    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    security_descriptor.SetSecurityDescriptorOwner(user_sid, False)
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    security_descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = security_descriptor
    return attributes


def _new_overlapped():
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    return overlapped


def _wait_overlapped_result(handle, overlapped, hr):
    """Block only the calling thread for this one operation's completion --
    other threads' own overlapped operations on the same handle proceed
    independently. Raises ConnectionClosedError on any pipe-level failure,
    including a cancelled op (used to unblock a pending accept on stop())."""
    try:
        if hr == winerror.ERROR_IO_PENDING:
            win32event.WaitForSingleObject(overlapped.hEvent, win32event.INFINITE)
        return win32file.GetOverlappedResult(handle, overlapped, False)
    except pywintypes.error as error:
        raise ipc_protocol.ConnectionClosedError(str(error)) from error


def _read_exact(handle, n):
    if n == 0:
        return b""
    chunks = []
    remaining = n
    while remaining > 0:
        overlapped = _new_overlapped()
        try:
            try:
                hr, data = win32file.ReadFile(handle, remaining, overlapped)
            except pywintypes.error as error:
                raise ipc_protocol.ConnectionClosedError(str(error)) from error
            transferred = _wait_overlapped_result(handle, overlapped, hr)
        finally:
            win32file.CloseHandle(overlapped.hEvent)
        if transferred == 0:
            raise ipc_protocol.ConnectionClosedError("peer closed the pipe")
        chunks.append(bytes(data[:transferred]))
        remaining -= transferred
    return b"".join(chunks)


def _write_all(handle, data):
    overlapped = _new_overlapped()
    try:
        try:
            hr, _submitted = win32file.WriteFile(handle, data, overlapped)
        except pywintypes.error as error:
            raise ipc_protocol.ConnectionClosedError(str(error)) from error
        _wait_overlapped_result(handle, overlapped, hr)
    finally:
        win32file.CloseHandle(overlapped.hEvent)


def _require_str(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise _RequestError(f"invalid-{key}")
    return value


_SUMMARY_FIELD_NAMES = tuple(f.name for f in dataclasses.fields(models.HistoryEntrySummary))


def _summary_to_dict(summary: models.HistoryEntrySummary) -> dict:
    """Only ever pulls the summary-declared fields, even when `summary` is
    actually a `HistoryEntryDetail` instance (a subclass with extra fields
    including raw `image_bytes`) -- `dataclasses.asdict(summary)` would pull
    every field of the *actual* object's class, leaking non-JSON-safe bytes
    into a plain summary/list response."""
    data = {name: getattr(summary, name) for name in _SUMMARY_FIELD_NAMES}
    data["created_at_utc"] = summary.created_at_utc.isoformat()
    data["updated_at_utc"] = summary.updated_at_utc.isoformat()
    return data


def _detail_to_dict(detail: models.HistoryEntryDetail) -> dict:
    """Everything except raw image bytes matches `_summary_to_dict`'s shape;
    image bytes are base64'd for JSON transport. File batches only ever
    expose name/size here -- actual file bytes never cross this channel, the
    existing `save_received_files` download path is main-process-only."""
    import base64

    data = _summary_to_dict(detail)
    data["text"] = detail.text
    data["url"] = detail.url
    data["files"] = [dataclasses.asdict(item) for item in detail.files]
    data["downloaded_directory"] = detail.downloaded_directory
    data["image_base64"] = (
        base64.b64encode(detail.image_bytes).decode("ascii")
        if detail.image_bytes is not None
        else None
    )
    return data


class _Session:
    """One authenticated connection's request/response and outbound
    control/event traffic. A single writer lock keeps pushed events and
    request replies from interleaving mid-frame on the shared pipe."""

    def __init__(self, handle):
        self._handle = handle
        self._write_lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._pending_acks = {}
        self._next_ack_id = 0

    def send(self, payload: dict) -> None:
        frame = ipc_protocol.encode_frame(payload)
        with self._write_lock:
            _write_all(self._handle, frame)

    def _read(self) -> dict:
        return ipc_protocol.read_frame(lambda n: _read_exact(self._handle, n))

    def authenticate(self, expected_token: bytes) -> bool:
        try:
            frame = self._read()
        except (
            ipc_protocol.FrameTooLargeError,
            ipc_protocol.MalformedFrameError,
            ipc_protocol.ConnectionClosedError,
        ) as error:
            logging.warning(
                "History IPC: rejected connection during auth (%s)", type(error).__name__
            )
            return False
        if frame.get("type") != ipc_protocol.TYPE_AUTH:
            logging.warning("History IPC: rejected connection (first frame was not auth)")
            return False
        token_hex = frame.get("token")
        if not isinstance(token_hex, str):
            logging.warning("History IPC: rejected connection (missing token)")
            return False
        try:
            token = bytes.fromhex(token_hex)
        except ValueError:
            logging.warning("History IPC: rejected connection (malformed token encoding)")
            return False
        if not hmac.compare_digest(token, expected_token):
            logging.warning("History IPC: rejected connection (wrong session token)")
            return False
        return True

    def serve(self, dispatch) -> None:
        self.send({"type": ipc_protocol.TYPE_AUTH_OK, "v": ipc_protocol.PROTOCOL_VERSION})
        while True:
            try:
                frame = self._read()
            except ipc_protocol.ConnectionClosedError:
                return
            except (ipc_protocol.FrameTooLargeError, ipc_protocol.MalformedFrameError) as error:
                logging.warning(
                    "History IPC: closing connection after a bad frame (%s)",
                    type(error).__name__,
                )
                return

            frame_type = frame.get("type")
            if frame_type == ipc_protocol.TYPE_FOCUS_ACK:
                self._resolve_ack(frame.get("ack_id"))
                continue
            if frame_type != ipc_protocol.TYPE_REQUEST:
                continue

            request_id = frame.get("id")
            action = frame.get("action")
            args = frame.get("args") or {}
            try:
                result = dispatch(action, args)
                response = {
                    "type": ipc_protocol.TYPE_RESPONSE,
                    "id": request_id,
                    "ok": True,
                    "result": result,
                }
            except _RequestError as error:
                response = {
                    "type": ipc_protocol.TYPE_RESPONSE,
                    "id": request_id,
                    "ok": False,
                    "error": error.code,
                }
            except Exception:
                logging.exception(
                    "History IPC: request handler failed (action=%s)", action
                )
                response = {
                    "type": ipc_protocol.TYPE_RESPONSE,
                    "id": request_id,
                    "ok": False,
                    "error": "internal-error",
                }
            try:
                self.send(response)
            except ipc_protocol.FrameTooLargeError:
                fallback = {
                    "type": ipc_protocol.TYPE_RESPONSE,
                    "id": request_id,
                    "ok": False,
                    "error": "response-too-large",
                }
                try:
                    self.send(fallback)
                except ipc_protocol.ConnectionClosedError:
                    return
            except ipc_protocol.ConnectionClosedError:
                return

    def request_ack(self, name: str, timeout_s: float) -> bool:
        with self._ack_lock:
            self._next_ack_id += 1
            ack_id = self._next_ack_id
            event = threading.Event()
            self._pending_acks[ack_id] = event
        try:
            self.send({"type": ipc_protocol.TYPE_CONTROL, "name": name, "ack_id": ack_id})
        except (ipc_protocol.ConnectionClosedError, ipc_protocol.FrameTooLargeError):
            with self._ack_lock:
                self._pending_acks.pop(ack_id, None)
            return False
        got = event.wait(timeout_s)
        with self._ack_lock:
            self._pending_acks.pop(ack_id, None)
        return got

    def _resolve_ack(self, ack_id):
        with self._ack_lock:
            event = self._pending_acks.get(ack_id)
        if event is not None:
            event.set()


class HistoryIpcServer:
    def __init__(self, service=None):
        _require_windows()
        self._service = service
        self.pipe_name = new_pipe_name()
        self._pipe_path = r"\\.\pipe\%s" % self.pipe_name
        self._security_attributes = _build_pipe_security_attributes()
        self._lock = threading.Lock()
        self._session = None
        self._current_pipe_handle = None
        self._expected_token = new_session_token()
        self._event_seq = 0
        self._stopping = False
        self._thread = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._accept_loop, name="clipcascade-history-ipc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Cancel whatever I/O the accept thread is currently blocked in
        (waiting for a connection, authenticating, or serving requests) so
        it unblocks and exits promptly rather than waiting for a client that
        may never arrive or send another frame."""
        self._stopping = True
        with self._lock:
            handle = self._current_pipe_handle
        if handle is not None:
            try:
                win32file.CancelIoEx(handle)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def begin_session(self):
        """Rotate the session token for the *next* child launch; returns
        (pipe_name, token_hex) to pass to the child via its environment. A
        stale/crashed child still holding a previous token can never
        authenticate against the new one."""
        with self._lock:
            self._expected_token = new_session_token()
            return self.pipe_name, self._expected_token.hex()

    def has_active_session(self) -> bool:
        with self._lock:
            return self._session is not None

    # --- accept loop -----------------------------------------------------

    def _accept_loop(self):
        while not self._stopping:
            try:
                handle = win32pipe.CreateNamedPipe(
                    self._pipe_path,
                    win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                    win32pipe.PIPE_TYPE_BYTE
                    | win32pipe.PIPE_READMODE_BYTE
                    | win32pipe.PIPE_WAIT,
                    1,
                    PIPE_BUFFER_SIZE,
                    PIPE_BUFFER_SIZE,
                    0,
                    self._security_attributes,
                )
            except Exception:
                logging.exception("History IPC: failed to create the named pipe")
                time.sleep(0.5)
                continue

            with self._lock:
                self._current_pipe_handle = handle
            try:
                if not self._wait_for_connection(handle):
                    continue
                if self._stopping:
                    return
                with self._lock:
                    expected_token = self._expected_token
                session = _Session(handle)
                if not session.authenticate(expected_token):
                    continue
                with self._lock:
                    self._session = session
                try:
                    session.serve(self._dispatch)
                finally:
                    with self._lock:
                        if self._session is session:
                            self._session = None
            except Exception:
                logging.exception("History IPC: connection handling failed")
            finally:
                with self._lock:
                    if self._current_pipe_handle is handle:
                        self._current_pipe_handle = None
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except Exception:
                    pass
                try:
                    win32file.CloseHandle(handle)
                except Exception:
                    pass

    def _wait_for_connection(self, handle) -> bool:
        """Overlapped ConnectNamedPipe: pywin32 returns the winerror code
        directly here rather than raising -- ERROR_PIPE_CONNECTED (535)
        means a client already connected between CreateNamedPipe and this
        call, which completed synchronously and needs no wait at all."""
        overlapped = _new_overlapped()
        try:
            result = win32pipe.ConnectNamedPipe(handle, overlapped)
            if result == _ERROR_PIPE_CONNECTED:
                return True
            win32event.WaitForSingleObject(overlapped.hEvent, win32event.INFINITE)
            if self._stopping:
                return False
            win32file.GetOverlappedResult(handle, overlapped, False)
            return True
        except pywintypes.error:
            if not self._stopping:
                logging.exception("History IPC: ConnectNamedPipe failed")
            return False
        finally:
            win32file.CloseHandle(overlapped.hEvent)

    # --- focus push (launcher-facing) -------------------------------------

    def request_focus(self, timeout_s: float = 2.0) -> bool:
        with self._lock:
            session = self._session
        if session is None:
            return False
        return session.request_ack("focus_window", timeout_s)

    # --- request dispatch --------------------------------------------------

    def _dispatch(self, action, args):
        if self._service is None:
            raise _RequestError("history-unavailable")
        if action == "ping":
            return {"pong": True}
        if action == "query":
            return self._handle_query(args)
        if action == "get_detail":
            return self._handle_get_detail(args)
        if action == "preview_retention":
            return self._handle_preview_retention(args)
        if action == "apply_retention":
            return self._handle_apply_retention(args)
        if action == "execute_command":
            return self._handle_execute_command(args)
        raise _RequestError("unknown-action")

    def _handle_query(self, args):
        page_size = args.get("page_size", ipc_protocol.DEFAULT_PAGE_SIZE)
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not (
            1 <= page_size <= ipc_protocol.MAX_PAGE_SIZE
        ):
            raise _RequestError("invalid-page-size")
        payload_types = args.get("payload_types")
        if payload_types is not None:
            if not isinstance(payload_types, list) or not all(
                pt in models.PAYLOAD_TYPES for pt in payload_types
            ):
                raise _RequestError("invalid-payload-types")
            payload_types = frozenset(payload_types)
        direction = args.get("direction")
        if direction is not None and direction not in models.DIRECTIONS:
            raise _RequestError("invalid-direction")
        cursor = args.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise _RequestError("invalid-cursor")
        filter_ = models.HistoryFilter(
            payload_types=payload_types,
            direction=direction,
            pinned_only=bool(args.get("pinned_only", False)),
        )
        page = self._service.query(filter_, cursor=cursor, page_size=page_size)
        return {
            "entries": [_summary_to_dict(entry) for entry in page.entries],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    def _handle_get_detail(self, args):
        entry_id = _require_str(args, "entry_id")
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise _RequestError("not-found")
        return _detail_to_dict(detail)

    def _parse_policy(self, args):
        raw = args.get("policy")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise _RequestError("invalid-policy")
        try:
            return models.RetentionPolicy(**raw)
        except TypeError as error:
            raise _RequestError("invalid-policy") from error

    def _handle_preview_retention(self, args):
        policy = self._parse_policy(args)
        impact = self._service.preview_retention(policy)
        return dataclasses.asdict(impact)

    def _handle_apply_retention(self, args):
        policy = self._parse_policy(args)
        result = self._service.apply_retention(policy)
        self.notify_retention_changed()
        return dataclasses.asdict(result)

    _COMMAND_BUILDERS = {
        "pin_entry": lambda args: models.PinEntryCommand(entry_id=_require_str(args, "entry_id")),
        "unpin_entry": lambda args: models.UnpinEntryCommand(
            entry_id=_require_str(args, "entry_id")
        ),
        "delete_entry": lambda args: models.DeleteEntryCommand(
            entry_id=_require_str(args, "entry_id")
        ),
        "clear_unpinned": lambda args: models.ClearUnpinnedCommand(),
        "clear_all": lambda args: models.ClearAllCommand(),
        "expire_transfers_now": lambda args: models.ExpireTransfersNowCommand(),
        "set_recording_enabled": lambda args: models.SetRecordingEnabledCommand(
            enabled=bool(args.get("enabled"))
        ),
    }

    def _handle_execute_command(self, args):
        name = args.get("command")
        builder = self._COMMAND_BUILDERS.get(name)
        if builder is None:
            raise _RequestError("unknown-command")
        try:
            command = builder(args)
        except _RequestError:
            raise
        except Exception as error:
            raise _RequestError("invalid-command-args") from error
        result = self._service.execute(command)
        if result.ok:
            self._emit_command_event(name, command)
        return dataclasses.asdict(result)

    def _emit_command_event(self, name, command):
        if name in ("pin_entry", "unpin_entry"):
            self.notify_entry_updated(command.entry_id)
        elif name == "delete_entry":
            self.notify_entry_deleted(command.entry_id)
        elif name in ("clear_unpinned", "clear_all", "expire_transfers_now"):
            self.notify_retention_changed()
        elif name == "set_recording_enabled":
            self.notify_service_disabled(not command.enabled)

    # --- event publishing (server -> client push) -------------------------

    def _next_seq(self):
        with self._lock:
            self._event_seq += 1
            return self._event_seq

    def _publish(self, name, data):
        with self._lock:
            session = self._session
        if session is None:
            return
        seq = self._next_seq()
        try:
            session.send(
                {"type": ipc_protocol.TYPE_EVENT, "seq": seq, "name": name, "data": data}
            )
        except ipc_protocol.ConnectionClosedError:
            pass
        except ipc_protocol.FrameTooLargeError:
            logging.warning("History IPC: dropped an oversized %s event", name)

    def notify_entry_added(self, entry_id) -> None:
        self._publish("entry_added", {"entry_id": entry_id})

    def notify_entry_updated(self, entry_id) -> None:
        self._publish("entry_updated", {"entry_id": entry_id})

    def notify_entry_deleted(self, entry_id) -> None:
        self._publish("entry_deleted", {"entry_id": entry_id})

    def notify_retention_changed(self) -> None:
        self._publish("retention_changed", {})

    def notify_service_disabled(self, disabled: bool) -> None:
        self._publish("service_disabled", {"disabled": bool(disabled)})
