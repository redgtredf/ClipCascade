"""Starts and focuses the on-demand history child process.

Owned by the main ClipCascade process. It never imports Qt and never uses a
shell: the child is spawned as an argument vector so no part of a clipboard
payload or a user path can be interpreted as a command.

T0 provides open/focus/shutdown only. Tray entry, global shortcut and the
command protocol arrive in later tickets.
"""

import os
import subprocess
import sys

from history_ui import channel, cli

FOCUS_TIMEOUT_S = 2.0
TERMINATE_TIMEOUT_S = 3.0

OUTCOME_FOCUSED = "focused"
OUTCOME_LAUNCHED = "launched"
OUTCOME_RESTARTED = "restarted"
OUTCOME_UNAVAILABLE = "unavailable"

# Keeps the child out of a console window when ClipCascade runs from source.
_CREATE_NO_WINDOW = 0x08000000


def child_command():
    """Argument vector for the history child, frozen or from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, cli.CHILD_MODE_FLAG]
    entry_point = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
    )
    return [sys.executable, entry_point, cli.CHILD_MODE_FLAG]


class HistoryProcessLauncher:
    def __init__(self, command=None, spawn=None, focus=None):
        self._command = command
        self._spawn = spawn or self._default_spawn
        self._focus = focus or (lambda: channel.request_focus(FOCUS_TIMEOUT_S))
        self._process = None
        self.last_error = None

    @staticmethod
    def _default_spawn(command):
        creation_flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(command, shell=False, creationflags=creation_flags)

    @property
    def process(self):
        return self._process

    def command(self):
        return list(self._command) if self._command else child_command()

    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def last_exit_code(self):
        """Exit code of the child we started, or None while it is alive."""
        if self._process is None:
            return None
        return self._process.poll()

    def open_or_focus(self):
        """Open the history window, or raise the one already showing."""
        if self.is_running():
            if self._focus():
                return OUTCOME_FOCUSED
            # Our child is alive but not answering: replace only that process.
            self.shutdown()
            return OUTCOME_RESTARTED if self._start() else OUTCOME_UNAVAILABLE

        # A window may still be open from an earlier launcher instance.
        if self._focus():
            return OUTCOME_FOCUSED
        return OUTCOME_LAUNCHED if self._start() else OUTCOME_UNAVAILABLE

    def _start(self):
        try:
            self._process = self._spawn(self.command())
        except OSError as error:
            self.last_error = error
            self._process = None
            return False
        self.last_error = None
        return True

    def shutdown(self):
        """Stop only the history child; ClipCascade itself is unaffected."""
        if self._process is None:
            return
        if self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(TERMINATE_TIMEOUT_S)
            except Exception as error:
                self.last_error = error
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None
