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

    def spawn(command):
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
        spawn=lambda _command: next(children),
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
        spawn=lambda _command: (_ for _ in ()).throw(OSError("blocked")),
        focus=lambda: False,
    )

    assert launcher.open_or_focus() == OUTCOME_UNAVAILABLE
    assert isinstance(launcher.last_error, OSError)


def test_history_requirements_pin_matching_essential_and_shiboken_versions():
    requirements = (
        Path(__file__).parents[1] / "src" / "requirements_win_history_ui.txt"
    ).read_text(encoding="utf-8")

    assert "PySide6-Essentials==6.8.2.1" in requirements
    assert "shiboken6==6.8.2.1" in requirements
