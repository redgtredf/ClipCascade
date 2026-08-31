"""Authenticated IPC client for the history child process.

Connects to the main ClipCascade process's named pipe using the pipe name
and per-launch session token the launcher passed through the environment
(never argv, never a log line -- see `ENV_PIPE_NAME`/`ENV_SESSION_TOKEN`).

This module never imports SQLite, DPAPI or the history storage/service code
-- it only ever sees whatever JSON the main process chooses to send, so the
child process has no way to reach the master key or write the database
directly. It also deliberately never imports pywin32: it's built on raw
ctypes Win32 calls, matching the rest of `history_ui/`'s minimal-import style
(the child's startup cost is a first-class concern -- see T0's evidence).

The pipe is opened with `FILE_FLAG_OVERLAPPED`; every read/write below issues
a fresh `OVERLAPPED` + event and waits only for that one operation. This is
required, not an optimization: a synchronous (non-overlapped) handle
serializes all I/O through the single handle, so the background reader
thread's blocking read and a `_request()` call's write from the caller's own
thread would deadlock each other on a plain synchronous pipe -- proven live
during development before this module used overlapped I/O.
"""

import ctypes
import itertools
import logging
import os
import threading
import time

from history import ipc_protocol

WINDOWS = os.name == "nt"

ENV_PIPE_NAME = "CLIPCASCADE_HISTORY_PIPE"
ENV_SESSION_TOKEN = "CLIPCASCADE_HISTORY_TOKEN"

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231
_RETRYABLE_CONNECT_ERRORS = (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY)
_ERROR_IO_PENDING = 997
_INFINITE = 0xFFFFFFFF

DEFAULT_CONNECT_TIMEOUT_S = 2.0
DEFAULT_REQUEST_TIMEOUT_S = 5.0


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class HistoryIpcClientError(RuntimeError):
    """A request failed, timed out, or the connection was not usable."""


def credentials_from_environment(env=None):
    """(pipe_name, token_bytes) from the launcher-provided environment, or
    (None, None) when this process was not started with a history session
    (e.g. a bare manual invocation, or history is disabled/unavailable)."""
    env = env if env is not None else os.environ
    pipe_name = env.get(ENV_PIPE_NAME)
    token_hex = env.get(ENV_SESSION_TOKEN)
    if not pipe_name or not token_hex:
        return None, None
    try:
        token = bytes.fromhex(token_hex)
    except ValueError:
        return None, None
    return pipe_name, token


def pipe_path(pipe_name: str) -> str:
    return r"\\.\pipe\%s" % pipe_name


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _connect_raw(pipe_name, timeout_s):
    """Retries on both ERROR_PIPE_BUSY (another client holds the single
    instance right now) and ERROR_FILE_NOT_FOUND -- the launcher spawns the
    child immediately after `begin_session()` returns, and the server's
    accept thread has not necessarily reached `CreateNamedPipe` yet by the
    time the child's first connect attempt runs, so the pipe may not exist
    in the kernel namespace for a brief moment. Both are transient; anything
    else (a genuinely wrong/missing pipe) is not worth retrying."""
    kernel32 = _kernel32()
    kernel32.CreateFileW.restype = ctypes.c_void_p
    deadline = time.monotonic() + timeout_s
    path = pipe_path(pipe_name)
    while True:
        handle = kernel32.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle not in (None, 0, _INVALID_HANDLE_VALUE):
            return handle
        error = ctypes.get_last_error()
        if error not in _RETRYABLE_CONNECT_ERRORS or time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


class _RawPipe:
    """Read/write over an overlapped Win32 named-pipe handle via plain
    ctypes. Each call blocks only the calling thread for that one
    operation's completion (via its own OVERLAPPED + event) -- required so
    the background reader thread's blocked read never prevents a different
    thread's write on the same handle (see module docstring)."""

    def __init__(self, handle):
        self._kernel32 = _kernel32()
        self._handle = handle

    def _new_overlapped(self):
        overlapped = _Overlapped()
        overlapped.hEvent = self._kernel32.CreateEventW(None, True, False, None)
        return overlapped

    def _wait_result(self, overlapped, ok):
        """`ok` is the immediate ReadFile/WriteFile return; False with
        ERROR_IO_PENDING means "still in flight, wait for it" -- any other
        False is a real failure. Returns the actual bytes transferred."""
        error = 0 if ok else ctypes.get_last_error()
        if not ok and error != _ERROR_IO_PENDING:
            raise ipc_protocol.ConnectionClosedError(f"pipe I/O failed (error {error})")
        if not ok:
            self._kernel32.WaitForSingleObject(overlapped.hEvent, _INFINITE)
        transferred = ctypes.c_uint32(0)
        got = self._kernel32.GetOverlappedResult(
            ctypes.c_void_p(self._handle), ctypes.byref(overlapped), ctypes.byref(transferred), False
        )
        if not got:
            raise ipc_protocol.ConnectionClosedError("pipe closed while completing I/O")
        return transferred.value

    def read_exact(self, n):
        if n == 0:
            return b""
        chunks = []
        remaining = n
        while remaining > 0:
            buffer = ctypes.create_string_buffer(remaining)
            read = ctypes.c_uint32(0)
            overlapped = self._new_overlapped()
            try:
                ok = self._kernel32.ReadFile(
                    ctypes.c_void_p(self._handle),
                    buffer,
                    remaining,
                    ctypes.byref(read),
                    ctypes.byref(overlapped),
                )
                transferred = self._wait_result(overlapped, ok)
            finally:
                self._kernel32.CloseHandle(overlapped.hEvent)
            if transferred == 0:
                raise ipc_protocol.ConnectionClosedError("pipe closed while reading")
            chunks.append(buffer.raw[:transferred])
            remaining -= transferred
        return b"".join(chunks)

    def write_all(self, data):
        written = ctypes.c_uint32(0)
        overlapped = self._new_overlapped()
        try:
            ok = self._kernel32.WriteFile(
                ctypes.c_void_p(self._handle),
                data,
                len(data),
                ctypes.byref(written),
                ctypes.byref(overlapped),
            )
            self._wait_result(overlapped, ok)
        finally:
            self._kernel32.CloseHandle(overlapped.hEvent)

    def close(self):
        try:
            self._kernel32.CancelIoEx(ctypes.c_void_p(self._handle), None)
        except Exception:
            pass
        try:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        except Exception:
            pass


class HistoryIpcClient:
    """Synchronous request/response API plus a background reader thread that
    dispatches server-pushed control/event frames. `on_event`/`on_focus`/
    `on_disconnected` run on the reader thread -- callers that need to touch
    Qt objects must marshal back onto the Qt thread themselves (a blocking
    invoke if the caller wants the ack to prove the Qt thread is actually
    alive, as `history_ui.main` does for `on_focus`)."""

    def __init__(self, pipe_name, token, on_event=None, on_focus=None, on_disconnected=None):
        self._pipe_name = pipe_name
        self._token = token
        self._on_event = on_event
        self._on_focus = on_focus
        self._on_disconnected = on_disconnected
        self._pipe = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._next_id = itertools.count(1)
        self._reader_thread = None
        self._connected = False
        self._last_seq = None

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> bool:
        handle = _connect_raw(self._pipe_name, timeout_s)
        if handle is None:
            return False
        pipe = _RawPipe(handle)
        try:
            frame = ipc_protocol.encode_frame(
                {"type": ipc_protocol.TYPE_AUTH, "token": self._token.hex()}
            )
            pipe.write_all(frame)
            response = ipc_protocol.read_frame(pipe.read_exact)
        except (
            ipc_protocol.MalformedFrameError,
            ipc_protocol.FrameTooLargeError,
            ipc_protocol.ConnectionClosedError,
        ):
            pipe.close()
            return False
        if response.get("type") != ipc_protocol.TYPE_AUTH_OK:
            pipe.close()
            return False
        self._pipe = pipe
        self._connected = True
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="clipcascade-history-ipc-client", daemon=True
        )
        self._reader_thread.start()
        return True

    def close(self) -> None:
        self._connected = False
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None
        thread = self._reader_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._reader_thread = None
        self._fail_all_pending("closed")

    # --- outgoing --------------------------------------------------------

    def _send_frame(self, payload):
        if self._pipe is None:
            raise ipc_protocol.ConnectionClosedError("not connected")
        frame = ipc_protocol.encode_frame(payload)
        with self._write_lock:
            self._pipe.write_all(frame)

    def _request(self, action, args=None, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        if not self._connected:
            raise HistoryIpcClientError("not connected")
        request_id = next(self._next_id)
        event = threading.Event()
        box = {}
        with self._pending_lock:
            self._pending[request_id] = (event, box)
        try:
            self._send_frame(
                {
                    "type": ipc_protocol.TYPE_REQUEST,
                    "id": request_id,
                    "action": action,
                    "args": args or {},
                }
            )
        except (ipc_protocol.ConnectionClosedError, ipc_protocol.FrameTooLargeError) as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise HistoryIpcClientError(f"send failed: {error}") from error
        if not event.wait(timeout_s):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise HistoryIpcClientError(f"timed out waiting for {action!r}")
        if "error" in box:
            raise HistoryIpcClientError(box["error"])
        response = box["response"]
        if not response.get("ok"):
            raise HistoryIpcClientError(response.get("error") or "unknown-error")
        return response.get("result")

    def ping(self, timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> bool:
        try:
            result = self._request("ping", timeout_s=timeout_s)
        except HistoryIpcClientError:
            return False
        return bool(result and result.get("pong"))

    def query(
        self,
        payload_types=None,
        direction=None,
        pinned_only=False,
        cursor=None,
        page_size=ipc_protocol.DEFAULT_PAGE_SIZE,
        timeout_s=DEFAULT_REQUEST_TIMEOUT_S,
    ):
        args = {
            "payload_types": list(payload_types) if payload_types else None,
            "direction": direction,
            "pinned_only": pinned_only,
            "cursor": cursor,
            "page_size": page_size,
        }
        return self._request("query", args, timeout_s=timeout_s)

    def get_detail(self, entry_id, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("get_detail", {"entry_id": entry_id}, timeout_s=timeout_s)

    def preview_retention(self, policy=None, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("preview_retention", {"policy": policy}, timeout_s=timeout_s)

    def apply_retention(self, policy=None, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("apply_retention", {"policy": policy}, timeout_s=timeout_s)

    def execute_command(self, command, timeout_s=DEFAULT_REQUEST_TIMEOUT_S, **fields):
        args = {"command": command}
        args.update(fields)
        return self._request("execute_command", args, timeout_s=timeout_s)

    def copy_again(self, entry_id, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("copy_again", {"entry_id": entry_id}, timeout_s=timeout_s)

    def download_files(
        self, entry_id, target_directory, filenames=None, timeout_s=DEFAULT_REQUEST_TIMEOUT_S
    ):
        return self._request(
            "download_files",
            {
                "entry_id": entry_id,
                "target_directory": target_directory,
                "filenames": list(filenames) if filenames is not None else None,
            },
            timeout_s=timeout_s,
        )

    def save_image(self, entry_id, target_path, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request(
            "save_image",
            {"entry_id": entry_id, "target_path": target_path},
            timeout_s=timeout_s,
        )

    def open_folder(self, entry_id, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("open_folder", {"entry_id": entry_id}, timeout_s=timeout_s)

    def copy_file_paths(self, entry_id, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("copy_file_paths", {"entry_id": entry_id}, timeout_s=timeout_s)

    def clear_windows_clipboard(self, timeout_s=DEFAULT_REQUEST_TIMEOUT_S):
        return self._request("clear_windows_clipboard", {}, timeout_s=timeout_s)

    # --- incoming (reader thread) ------------------------------------------

    def _fail_all_pending(self, error):
        with self._pending_lock:
            waiting = list(self._pending.items())
            self._pending.clear()
        for _request_id, (event, box) in waiting:
            box["error"] = error
            event.set()

    def _read_loop(self):
        try:
            while self._connected:
                frame = ipc_protocol.read_frame(self._pipe.read_exact)
                self._handle_frame(frame)
        except (
            ipc_protocol.ConnectionClosedError,
            ipc_protocol.MalformedFrameError,
            ipc_protocol.FrameTooLargeError,
        ):
            pass
        except Exception:
            logging.exception("History IPC client: reader loop failed")
        finally:
            was_connected = self._connected
            self._connected = False
            self._fail_all_pending("disconnected")
            if was_connected and self._on_disconnected is not None:
                try:
                    self._on_disconnected()
                except Exception:
                    logging.exception("History IPC client: on_disconnected callback failed")

    def _handle_frame(self, frame):
        frame_type = frame.get("type")
        if frame_type == ipc_protocol.TYPE_RESPONSE:
            self._resolve_response(frame)
        elif frame_type == ipc_protocol.TYPE_EVENT:
            self._handle_event(frame)
        elif frame_type == ipc_protocol.TYPE_CONTROL:
            self._handle_control(frame)

    def _resolve_response(self, frame):
        request_id = frame.get("id")
        with self._pending_lock:
            entry = self._pending.pop(request_id, None)
        if entry is None:
            return
        event, box = entry
        box["response"] = frame
        event.set()

    def _handle_event(self, frame):
        """Sequence-gap detection: the first event a (re)connected client
        sees establishes the baseline (the server may already be well past
        seq 1), so only a *subsequent* non-consecutive seq counts as a gap."""
        seq = frame.get("seq")
        gap = False
        if isinstance(seq, int):
            if self._last_seq is not None and seq != self._last_seq + 1:
                gap = True
            self._last_seq = seq
        if self._on_event is not None:
            try:
                self._on_event(frame.get("name"), frame.get("data") or {}, gap)
            except Exception:
                logging.exception("History IPC client: on_event callback failed")

    def _handle_control(self, frame):
        name = frame.get("name")
        ack_id = frame.get("ack_id")
        if name == "focus_window" and self._on_focus is not None:
            try:
                self._on_focus()
            except Exception:
                logging.exception("History IPC client: on_focus callback failed")
        if ack_id is not None:
            try:
                self._send_frame({"type": ipc_protocol.TYPE_FOCUS_ACK, "ack_id": ack_id})
            except (ipc_protocol.ConnectionClosedError, ipc_protocol.FrameTooLargeError):
                pass
