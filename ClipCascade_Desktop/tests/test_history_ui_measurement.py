"""Source-level integration of the packaged open-to-first-page-ready
measurement: a real child process (``main.py --history-ui``) connecting to a
real authenticated IPC server over a real OS pipe, with a real encrypted
history store -- the same path the packaged gate measures, minus PyInstaller.

Asserts the child's measurement report carries the fields the 300 ms gate
consumes: window-present and first-page-ready, with a live data connection.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history IPC is Windows-only")

from history import ipc, models, service  # noqa: E402
from history_ui.client import ENV_PIPE_NAME, ENV_SESSION_TOKEN  # noqa: E402

MAIN_PY = Path(__file__).parents[1] / "src" / "main.py"

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402
import io  # noqa: E402


def _png_bytes(width=48, height=32):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(30, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed(service_instance, count):
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(count):
        payload_type = ("text", "text", "link-text", "image", "files")[i % 5]
        if payload_type == "image":
            payload = _png_bytes()
        elif payload_type == "files":
            payload = {f"file-{i}-a.txt": b"a" * 256, f"file-{i}-b.bin": b"b" * 512}
        elif payload_type == "link-text":
            # links are classified from text payloads by the service
            payload_type = "text"
            payload = f"https://example.com/item/{i}"
        else:
            payload = f"history entry {i}: the quick brown fox jumps over the lazy dog"
        service_instance.record(
            models.HistoryCaptureEvent(
                direction=("local", "remote")[i % 2],
                payload_type=payload_type,
                payload=payload,
                source_device_id=f"device-{i % 3}" if i % 2 else None,
                source_device_name=f"Device {i % 3}" if i % 2 else None,
                transport=("local", "p2s", "p2p")[i % 3],
                occurred_at_utc=base + timedelta(seconds=i * 10),
            )
        )


@pytest.fixture
def seeded_service(tmp_path):
    result = service.bootstrap(str(tmp_path / "history"))
    assert result.enabled, result.disabled_reason
    _seed(result.service, 120)
    yield result.service
    result.service.close()


def test_history_child_reports_first_page_ready_measurement(seeded_service, tmp_path):
    server = ipc.HistoryIpcServer(seeded_service)
    server.start()
    try:
        pipe_name, token_hex = server.begin_session()
        report_path = tmp_path / "measurement-report.json"

        env = dict(os.environ)
        env[ENV_PIPE_NAME] = pipe_name
        env[ENV_SESSION_TOKEN] = token_hex
        env["QT_QPA_PLATFORM"] = "offscreen"

        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                str(MAIN_PY),
                "--history-ui",
                "--hold-seconds",
                "1.5",
                "--report",
                str(report_path),
            ],
            env=env,
            timeout=60,
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - started

        assert completed.returncode == 0, completed.stderr[-2000:]

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["mode"] == "history-ui"
        assert report["ipc_connected"] is True, "child must use the real authenticated pipe"
        assert report["process_start_to_window_present_s"] >= 0
        assert report["process_start_to_first_page_ready_s"] >= 0, (
            "the 'ready' state must be measured when the first page of real data lands"
        )
        assert elapsed < 60
    finally:
        server.stop()
