"""Starts and focuses the on-demand history child process over the
authenticated IPC channel in `history/ipc.py`.

Owned by the main ClipCascade process. It never imports Qt and never uses a
shell: the child is spawned as an argument vector so no part of a clipboard
payload or a user path can be interpreted as a command. The per-launch pipe
name and session token travel through the child's environment only -- never
argv, never a log line (see `history_ui.client.ENV_PIPE_NAME`/
`ENV_SESSION_TOKEN`).

The main process is always the IPC server (it owns `HistoryService`); a
"please focus yourself" request is therefore just another push over the
already-authenticated connection, not a second channel -- see
`HistoryIpcServer.request_focus`, which this launcher's default `_focus`
delegates to and which also detects an unresponsive (hung) child by timing
out waiting for its ack.
"""

import os
import subprocess
import sys
import time

from history_ui import cli
from history_ui.client import ENV_PIPE_NAME, ENV_SESSION_TOKEN

FOCUS_TIMEOUT_S = 2.0
TERMINATE_TIMEOUT_S = 3.0

# A crash-looping or permanently-hung child must never restart forever: after
# this many consecutive unresponsive-child replacements without a clean focus
# or a fresh open in between, further opens report unavailable instead of
# spawning again. The streak resets once enough time has passed, so a
# transient bad patch doesn't lock the feature out permanently.
MAX_CONSECUTIVE_RESTARTS = 3
RESTART_COOLDOWN_S = 60.0

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
    def __init__(self, command=None, spawn=None, focus=None, ipc_server=None, clock=None):
        self._command = command
        self._spawn = spawn or self._default_spawn
        self._ipc_server = ipc_server
        self._focus = focus or self._default_focus
        self._clock = clock or time.monotonic
        self._process = None
        self._restart_streak = 0
        self._last_restart_at = None
        self.last_error = None

    @staticmethod
    def _default_spawn(command, env=None):
        creation_flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(command, shell=False, creationflags=creation_flags, env=env)

    def _default_focus(self):
        if self._ipc_server is None:
            return False
        return self._ipc_server.request_focus(FOCUS_TIMEOUT_S)

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

    def _reset_restart_budget_if_cooled_down(self):
        if self._last_restart_at is not None and self._clock() - self._last_restart_at > RESTART_COOLDOWN_S:
            self._restart_streak = 0

    def open_or_focus(self):
        """Open the history window, or raise the one already showing."""
        if self.is_running():
            if self._focus():
                self._restart_streak = 0
                return OUTCOME_FOCUSED
            # Our child is alive but not answering: replace only that
            # process, bounded so a crash-looping/hung child can never
            # restart forever.
            self.shutdown()
            return self._replace_unresponsive_child()

        # A window may still be open from an earlier launcher instance.
        if self._focus():
            self._restart_streak = 0
            return OUTCOME_FOCUSED
        return self._fresh_start()

    def _restart_budget_exhausted(self):
        """Shared gate for both branches: once a run of consecutive
        replacements has exhausted the budget, a crash-looping/permanently
        hung child must keep reporting unavailable rather than spawning
        again -- including on a *later* call where `self._process` has
        already been cleared by `shutdown()` and `is_running()` is now
        False, which would otherwise silently bypass the bound."""
        self._reset_restart_budget_if_cooled_down()
        return self._restart_streak >= MAX_CONSECUTIVE_RESTARTS

    def _replace_unresponsive_child(self):
        """Consumes one unit of the restart budget. Spawning a process
        almost always "succeeds" at the OS level even when the child goes
        on to hang, so the streak is deliberately not cleared just because
        `_start()` returned True -- only an actual acknowledged focus
        (OUTCOME_FOCUSED, above) proves the child is healthy and resets it."""
        if self._restart_budget_exhausted():
            return OUTCOME_UNAVAILABLE
        self._restart_streak += 1
        self._last_restart_at = self._clock()
        return OUTCOME_RESTARTED if self._start() else OUTCOME_UNAVAILABLE

    def _fresh_start(self):
        """Nothing of ours is currently running, so this isn't itself a
        "restart" and doesn't consume budget -- but it still must respect an
        already-exhausted budget (see `_restart_budget_exhausted`)."""
        if self._restart_budget_exhausted():
            return OUTCOME_UNAVAILABLE
        return OUTCOME_LAUNCHED if self._start() else OUTCOME_UNAVAILABLE

    def _start(self):
        env = None
        if self._ipc_server is not None:
            try:
                pipe_name, token_hex = self._ipc_server.begin_session()
            except Exception as error:
                self.last_error = error
                return False
            env = dict(os.environ)
            env[ENV_PIPE_NAME] = pipe_name
            env[ENV_SESSION_TOKEN] = token_hex
        try:
            self._process = self._spawn(self.command(), env=env)
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

    def shutdown_ipc(self):
        """Stop the child (if any) and the authenticated IPC server behind
        it. Call once, at application exit."""
        self.shutdown()
        if self._ipc_server is not None:
            try:
                self._ipc_server.stop()
            except Exception:
                pass
