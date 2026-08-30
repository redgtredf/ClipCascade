"""Child-mode command line handled before the normal application starts.

`dispatch` returns ``None`` for an ordinary ClipCascade launch and an exit code
when the argument vector selects the history child process or one of the
packaging probes. Nothing here imports Qt at module scope, so the main process
pays nothing for a history UI it never opens.

`main.py` mirrors the two flag constants below so that it can decide whether to
import this module at all; ``tests/test_history_ui_cli.py`` keeps them in step.
"""

import argparse
import ctypes
import json
import os
import sys
import time

CHILD_MODE_FLAG = "--history-ui"
STARTUP_PROBE_FLAG = "--startup-probe"

EXIT_OK = 0
EXIT_UI_UNAVAILABLE = 3
EXIT_FOCUS_FAILED = 4

WINDOWS = sys.platform == "win32"


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


def _filetime_ticks(filetime):
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


def elapsed_since_process_start():
    """Seconds since this process was created, or None when unavailable.

    Measured from the Win32 process creation time so the number includes the
    PyInstaller bootloader work that happens before Python starts.
    """
    if not WINDOWS:
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(_FILETIME)]
        created = _FILETIME()
        exited = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        ok = kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        now = _FILETIME()
        kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))
        return (_filetime_ticks(now) - _filetime_ticks(created)) / 10_000_000.0
    except Exception:
        return None


def build_parser(prog):
    parser = argparse.ArgumentParser(prog=prog, add_help=False)
    parser.add_argument(CHILD_MODE_FLAG, action="store_true", dest="history_ui")
    parser.add_argument(STARTUP_PROBE_FLAG, action="store_true", dest="startup_probe")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Exit as soon as the first frame is on screen (measurement mode).",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        help="Stay alive this long before exiting, so memory can be sampled.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Write measurements as JSON to this path (the packaged build is "
        "windowed and has no stdout).",
    )
    return parser


def parse_options(argv):
    parser = build_parser(os.path.basename(sys.argv[0]) if sys.argv else "ClipCascade")
    options, _unknown = parser.parse_known_args(list(argv))
    return options


def write_report(path, payload):
    """Best-effort measurement dump; never fails the run it is measuring."""
    if not path:
        return
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as report_file:
            json.dump(payload, report_file, indent=2, sort_keys=True)
    except Exception as error:
        print(f"ClipCascade: could not write report {path}: {error}", file=sys.stderr)


def _runtime_facts():
    return {
        "pid": os.getpid(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }


def dispatch(argv):
    """Return an exit code for a child mode, or None for a normal launch."""
    argv = list(argv)
    if STARTUP_PROBE_FLAG in argv:
        return run_startup_probe(argv)
    if CHILD_MODE_FLAG in argv:
        return run_history_ui(argv)
    return None


def run_startup_probe(argv):
    """Measure packaged start-up cost without touching the history UI.

    Available in every Windows build, including one compiled without PySide6,
    so baseline and candidate executables are measured the same way.
    """
    options = parse_options(argv)
    facts = _runtime_facts()
    facts["mode"] = "startup-probe"
    facts["process_start_to_ready_s"] = elapsed_since_process_start()
    write_report(options.report, facts)
    if options.hold_seconds > 0:
        time.sleep(options.hold_seconds)
    return EXIT_OK


def run_history_ui(argv):
    """Start the PySide6 history child, or report that this build has none."""
    options = parse_options(argv)
    try:
        from history_ui.main import run
    except Exception as error:  # missing PySide6, or a Qt DLL that will not load
        print(
            f"ClipCascade: history window unavailable in this build: {error}",
            file=sys.stderr,
        )
        facts = _runtime_facts()
        facts["mode"] = "history-ui"
        facts["error"] = f"{type(error).__name__}: {error}"
        write_report(options.report, facts)
        return EXIT_UI_UNAVAILABLE
    return run(argv)
