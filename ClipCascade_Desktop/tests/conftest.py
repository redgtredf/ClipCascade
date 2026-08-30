import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.config import Config  # noqa: E402


class FakeClipboardMonitor:
    """Stands in for the platform clipboard monitor; never touches the host clipboard."""

    def __init__(self):
        self.on_update_calls = []
        self.stop_calls = 0

    def on_update(self, callback=None, enable_image_monitoring=True,
                  enable_file_monitoring=True):
        self.on_update_calls.append(
            {
                "callback": callback,
                "enable_image_monitoring": enable_image_monitoring,
                "enable_file_monitoring": enable_file_monitoring,
            }
        )

    def stop(self):
        self.stop_calls += 1

    def fire(self, type_, content):
        for registration in self.on_update_calls:
            registration["callback"](type_, content)


@pytest.fixture
def fake_monitor(monkeypatch):
    monitor = FakeClipboardMonitor()
    monkeypatch.setattr(
        "clipboard.clipboard_manager.clipboard_monitor", monitor
    )
    return monitor


@pytest.fixture
def tmp_config(tmp_path):
    return Config(file_name=str(tmp_path / "DATA"))


@pytest.fixture
def manager(tmp_config, fake_monitor):
    from clipboard.clipboard_manager import ClipboardManager

    return ClipboardManager(tmp_config)
