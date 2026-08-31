import logging
import threading

import utils.error_logging as error_logging


def test_rotating_logging_writes_records(tmp_path):
    log_path = tmp_path / "app.log"
    error_logging.setup_rotating_logging(str(log_path), level=logging.INFO)
    logging.getLogger("test").info("hello rotating")

    text = log_path.read_text(encoding="utf-8")
    assert "hello rotating" in text
    assert "test" in text
    assert "INFO" in text


def test_rotating_logging_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(error_logging, "LOG_MAX_BYTES", 200)
    log_path = tmp_path / "app.log"
    error_logging.setup_rotating_logging(str(log_path), level=logging.INFO)
    for i in range(50):
        logging.getLogger("test").info("line %s %s", i, "x" * 40)

    rotated = tmp_path / "app.log.1"
    assert rotated.exists()
    assert log_path.stat().st_size <= 200 + 120


def test_sys_excepthook_logs_uncaught_exception(tmp_path):
    log_path = tmp_path / "app.log"
    error_logging.setup_rotating_logging(str(log_path), level=logging.INFO)
    error_logging.install_excepthooks()

    try:
        raise ValueError("boom-main")
    except ValueError:
        import sys

        sys.excepthook(*sys.exc_info())

    text = log_path.read_text(encoding="utf-8")
    assert "Uncaught exception" in text
    assert "boom-main" in text
    assert "CRITICAL" in text


def test_threading_excepthook_logs_thread_crash(tmp_path):
    log_path = tmp_path / "app.log"
    error_logging.setup_rotating_logging(str(log_path), level=logging.INFO)
    error_logging.install_excepthooks()

    def crash():
        raise RuntimeError("boom-thread")

    thread = threading.Thread(target=crash, name="crasher")
    thread.start()
    thread.join()

    text = log_path.read_text(encoding="utf-8")
    assert "boom-thread" in text
    assert "crasher" in text


def test_tk_report_hook_logs_callback_failure(tmp_path):
    log_path = tmp_path / "app.log"
    error_logging.setup_rotating_logging(str(log_path), level=logging.INFO)

    class FakeRoot:
        pass

    root = FakeRoot()
    error_logging.install_tk_report_hook(root)
    assert callable(root.report_callback_exception)

    try:
        raise KeyError("boom-tk")
    except KeyError:
        import sys

        root.report_callback_exception(*sys.exc_info())

    text = log_path.read_text(encoding="utf-8")
    assert "Tkinter callback" in text
    assert "boom-tk" in text
    assert "ERROR" in text


def test_hooks_never_raise():
    class BrokenLogger:
        def critical(self, *args, **kwargs):
            raise OSError("log sink gone")

        def error(self, *args, **kwargs):
            raise OSError("log sink gone")

    error_logging.install_excepthooks(logger=BrokenLogger())
    try:
        raise ValueError("ignored")
    except ValueError:
        import sys

        sys.excepthook(*sys.exc_info())

    error_logging.install_tk_report_hook(
        type("R", (), {})(), logger=BrokenLogger()
    )
