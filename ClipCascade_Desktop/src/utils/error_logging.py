import logging
import logging.handlers
import sys
import threading

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def setup_rotating_logging(log_path, level=logging.INFO):
    """Route root logging into a size-capped rotating file.

    Replaces the previous per-run truncating basicConfig(filemode="w"):
    old runs keep LOG_BACKUP_COUNT rotated copies instead of being wiped.
    Safe to call once at startup; later calls are no-ops if already
    configured with the same file.
    """
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]


def install_excepthooks(logger=None):
    """Log uncaught exceptions from the main thread and any thread.

    Both hooks never raise: a failure inside logging itself must not turn
    one crash into a crash loop.
    """
    log = logger or logging.getLogger(__name__)

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            log.critical(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
            )
        except Exception:
            pass

    def _threading_hook(args):
        try:
            log.critical(
                "Uncaught exception in thread %s",
                getattr(args.thread, "name", args.thread),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            pass

    sys.excepthook = _sys_hook
    threading.excepthook = _threading_hook


def install_tk_report_hook(root, logger=None):
    """Log exceptions raised inside Tkinter callbacks.

    Tk swallows callback exceptions after printing to stderr; in a windowed
    (no-console) packaged build that output is lost entirely, so callbacks
    that fail leave no trace. This restores visibility for the tray root.
    """
    log = logger or logging.getLogger(__name__)

    def _report_callback_exception(exc_type, exc_value, exc_tb):
        try:
            log.error(
                "Uncaught exception in Tkinter callback",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            pass

    root.report_callback_exception = _report_callback_exception
