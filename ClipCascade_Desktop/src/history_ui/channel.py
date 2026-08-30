"""Focus handshake between ClipCascade and its history child process.

The child owns a per-user named pipe. Anyone who can open it may ask the window
to come forward; nothing else travels over it. The authenticated command
channel described in the technical plan replaces this in the process/IPC
ticket, so deliberately no history data, key material or file bytes are carried
here.

Both ends of the handshake live here: the client half uses plain Win32 calls so
the main ClipCascade process can raise the window without importing Qt.
"""

import ctypes
import getpass
import hashlib
import json
import os
import sys
import threading

WINDOWS = sys.platform == "win32"

PIPE_PREFIX = "ClipCascade.History"
GREETING_LIMIT = 4096
RESPONSE_LIMIT = 4096
DEFAULT_TIMEOUT_S = 2.0

ACTION_FOCUS = "focus"
ACTION_PING = "ping"

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ASFW_ANY = -1


def _current_user():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "unknown")


def pipe_name():
    """Per-user pipe name; the server also restricts the pipe to that user."""
    digest = hashlib.sha256(_current_user().encode("utf-8", "replace")).hexdigest()
    return f"{PIPE_PREFIX}.{digest[:16]}"


def pipe_path(name=None):
    return r"\\.\pipe\%s" % (name or pipe_name())


def allow_foreground(pid):
    """Let the child process take the foreground when we ask it to.

    Windows refuses SetForegroundWindow from a background process unless the
    current foreground process hands the right over first, so the requester
    grants it before sending the focus command.
    """
    if not WINDOWS:
        return False
    try:
        return bool(ctypes.windll.user32.AllowSetForegroundWindow(int(pid)))
    except Exception:
        return False


def _exchange(request, result):
    """Blocking client half of the handshake, run on a throwaway thread."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        pipe_path(),
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return
    try:
        greeting = _read_json(kernel32, handle, GREETING_LIMIT)
        if greeting is None:
            return
        peer_pid = greeting.get("pid")
        if peer_pid:
            allow_foreground(peer_pid)
        payload = (json.dumps(request) + "\n").encode("utf-8")
        written = ctypes.c_uint32(0)
        if not kernel32.WriteFile(
            ctypes.c_void_p(handle),
            payload,
            len(payload),
            ctypes.byref(written),
            None,
        ):
            return
        kernel32.FlushFileBuffers(ctypes.c_void_p(handle))
        response = _read_json(kernel32, handle, RESPONSE_LIMIT)
        if response is not None:
            response.setdefault("pid", peer_pid)
            result["response"] = response
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _read_json(kernel32, handle, limit):
    buffer = ctypes.create_string_buffer(limit)
    read = ctypes.c_uint32(0)
    if not kernel32.ReadFile(
        ctypes.c_void_p(handle), buffer, limit, ctypes.byref(read), None
    ):
        return None
    raw = buffer.raw[: read.value].split(b"\n", 1)[0]
    if not raw:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def send(action, timeout_s=DEFAULT_TIMEOUT_S):
    """Send one command to a running history window.

    Returns the response dict, or None when no window answered in time. The
    exchange runs on a daemon thread so an unresponsive child can never block
    clipboard synchronisation; the caller decides what to do after a timeout.
    """
    if not WINDOWS:
        return None
    result = {}
    worker = threading.Thread(
        target=_exchange,
        args=({"action": action}, result),
        name="clipcascade-history-focus",
        daemon=True,
    )
    worker.start()
    worker.join(timeout_s)
    response = result.get("response")
    if not response or not response.get("ok"):
        return None
    return response


def request_focus(timeout_s=DEFAULT_TIMEOUT_S):
    """True when a running history window accepted the focus request."""
    return send(ACTION_FOCUS, timeout_s) is not None


def ping(timeout_s=DEFAULT_TIMEOUT_S):
    """True when a history window is listening on this user's pipe."""
    return send(ACTION_PING, timeout_s) is not None
