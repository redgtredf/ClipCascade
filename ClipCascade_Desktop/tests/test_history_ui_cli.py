"""Focused T0 tests for the isolated PySide6 child bootstrap."""

import json
import subprocess
import sys
from pathlib import Path

from history_ui import cli
from history_ui.launcher import (
    OUTCOME_FOCUSED,
    OUTCOME_LAUNCHED,
    OUTCOME_RESTARTED,
    OUTCOME_UNAVAILABLE,
    HistoryProcessLauncher,
    child_command,
)


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = False
        self.waited_for = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout):
        self.waited_for = timeout
        return self.exit_code


def test_child_mode_flags_stay_in_sync_with_main_entry_point():
    main_source = (Path(__file__).parents[1] / "src" / "main.py").read_text(
        encoding="utf-8"
    )
    assert cli.CHILD_MODE_FLAG in main_source
    assert cli.STARTUP_PROBE_FLAG in main_source


def test_startup_probe_writes_measurement_without_importing_qt(tmp_path, monkeypatch):
    report = tmp_path / "startup.json"
    monkeypatch.setattr(cli, "elapsed_since_process_start", lambda: 0.125)
    qt_modules_before = {
        name
        for name in sys.modules
        if name == "PySide6" or name.startswith("PySide6.")
    }
    exit_code = cli.run_startup_probe(["--startup-probe", "--report", str(report)])

    assert exit_code == cli.EXIT_OK
    facts = json.loads(report.read_text(encoding="utf-8"))
    assert facts["mode"] == "startup-probe"
    assert facts["process_start_to_ready_s"] == 0.125
    assert {
        name
        for name in sys.modules
        if name == "PySide6" or name.startswith("PySide6.")
    } == qt_modules_before


def test_source_child_command_is_an_argument_vector(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    command = child_command()

    assert command[0] == sys.executable
    assert command[1].endswith("main.py")
    assert command[2] == cli.CHILD_MODE_FLAG


def test_default_spawn_never_uses_a_shell(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    HistoryProcessLauncher._default_spawn(["ClipCascade.exe", "--history-ui"])

    assert captured["command"] == ["ClipCascade.exe", "--history-ui"]
    assert captured["shell"] is False


def test_second_open_focuses_existing_child_without_spawning_again():
    child = FakeProcess()
    spawns = []

    def spawn(command, env=None):
        spawns.append(command)
        return child

    launcher = HistoryProcessLauncher(
        command=["ClipCascade.exe", "--history-ui"], spawn=spawn, focus=lambda: False
    )
    assert launcher.open_or_focus() == OUTCOME_LAUNCHED
    launcher._focus = lambda: True

    assert launcher.open_or_focus() == OUTCOME_FOCUSED
    assert len(spawns) == 1
    assert child.terminated is False


def test_unresponsive_child_is_replaced_without_terminating_parent():
    first = FakeProcess()
    second = FakeProcess()
    children = iter([first, second])
    launcher = HistoryProcessLauncher(
        command=["ClipCascade.exe", "--history-ui"],
        spawn=lambda _command, env=None: next(children),
        focus=lambda: False,
    )

    assert launcher.open_or_focus() == OUTCOME_LAUNCHED
    assert launcher.open_or_focus() == OUTCOME_RESTARTED
    assert first.terminated is True
    assert first.waited_for is not None
    assert launcher.process is second


def test_spawn_failure_reports_unavailable():
    launcher = HistoryProcessLauncher(
        command=["ClipCascade.exe", "--history-ui"],
        spawn=lambda _command, env=None: (_ for _ in ()).throw(OSError("blocked")),
        focus=lambda: False,
    )

    assert launcher.open_or_focus() == OUTCOME_UNAVAILABLE
    assert isinstance(launcher.last_error, OSError)


def test_restart_streak_is_bounded_against_a_permanently_hung_child():
    """A child that never acknowledges focus must eventually stop being
    restarted -- otherwise a permanently-hung child restarts forever."""
    from history_ui.launcher import MAX_CONSECUTIVE_RESTARTS

    children = iter(FakeProcess() for _ in range(MAX_CONSECUTIVE_RESTARTS + 5))
    clock = {"t": 0.0}
    launcher = HistoryProcessLauncher(
        command=["ClipCascade.exe", "--history-ui"],
        spawn=lambda _command, env=None: next(children),
        focus=lambda: False,  # never acknowledges -> always "unresponsive"
        clock=lambda: clock["t"],
    )

    assert launcher.open_or_focus() == OUTCOME_LAUNCHED
    outcomes = [launcher.open_or_focus() for _ in range(MAX_CONSECUTIVE_RESTARTS)]
    assert outcomes == [OUTCOME_RESTARTED] * MAX_CONSECUTIVE_RESTARTS

    # The budget is now exhausted: no further restart is attempted.
    assert launcher.open_or_focus() == OUTCOME_UNAVAILABLE
    assert launcher.open_or_focus() == OUTCOME_UNAVAILABLE


def test_restart_streak_resets_after_a_cooldown():
    from history_ui.launcher import MAX_CONSECUTIVE_RESTARTS, RESTART_COOLDOWN_S

    children = iter(FakeProcess() for _ in range(MAX_CONSECUTIVE_RESTARTS + 5))
    clock = {"t": 0.0}
    launcher = HistoryProcessLauncher(
        command=["ClipCascade.exe", "--history-ui"],
        spawn=lambda _command, env=None: next(children),
        focus=lambda: False,
        clock=lambda: clock["t"],
    )

    launcher.open_or_focus()
    for _ in range(MAX_CONSECUTIVE_RESTARTS):
        launcher.open_or_focus()
    assert launcher.open_or_focus() == OUTCOME_UNAVAILABLE

    # The child was already shut down by the exhausted attempt above, so the
    # next successful attempt is a fresh open (nothing of ours is running),
    # not a "replace a still-running-but-unresponsive child" restart.
    clock["t"] += RESTART_COOLDOWN_S + 1
    assert launcher.open_or_focus() == OUTCOME_LAUNCHED


def test_start_passes_a_fresh_session_pipe_and_token_via_environment():
    """The pipe name/session token must travel through the child's
    environment (never argv), and each launch must rotate the token."""
    from history_ui.client import ENV_PIPE_NAME, ENV_SESSION_TOKEN

    class FakeIpcServer:
        def __init__(self):
            self.sessions = []

        def begin_session(self):
            token_hex = f"token-{len(self.sessions)}"
            pipe_name = "ClipCascade.HistoryIpc.fake"
            self.sessions.append((pipe_name, token_hex))
            return pipe_name, token_hex

        def request_focus(self, timeout_s):
            return False

    captured_envs = []

    def spawn(command, env=None):
        captured_envs.append(env)
        assert command == child_command()
        return FakeProcess()

    ipc_server = FakeIpcServer()
    launcher = HistoryProcessLauncher(spawn=spawn, ipc_server=ipc_server)

    assert launcher.open_or_focus() == OUTCOME_LAUNCHED
    assert captured_envs[0][ENV_PIPE_NAME] == "ClipCascade.HistoryIpc.fake"
    assert captured_envs[0][ENV_SESSION_TOKEN] == "token-0"
    # The rest of the parent's environment (PATH etc.) must still be present.
    assert "PATH" in {k.upper() for k in captured_envs[0]}

    launcher.shutdown()
    assert launcher.open_or_focus() == OUTCOME_LAUNCHED
    assert captured_envs[1][ENV_SESSION_TOKEN] == "token-1"


def test_history_requirements_pin_matching_essential_and_shiboken_versions():
    requirements = (
        Path(__file__).parents[1] / "src" / "requirements_win_history_ui.txt"
    ).read_text(encoding="utf-8")

    assert "PySide6-Essentials==6.8.2.1" in requirements
    assert "shiboken6==6.8.2.1" in requirements
