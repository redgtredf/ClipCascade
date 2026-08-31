"""QApplication bootstrap for the on-demand history child process.

Started as ``ClipCascade.exe --history-ui``, always by `history_ui.launcher`,
which passes the authenticated IPC pipe name and per-launch session token
through the environment (`history_ui.client.ENV_PIPE_NAME`/
`ENV_SESSION_TOKEN`) -- never argv, never a log line. This module never
touches SQLite, DPAPI or the history master key: everything it shows comes
back already decrypted, over `history_ui.client.HistoryIpcClient`, mediated
by the read-only gateway (`history_ui.controller.ReadOnlyHistoryGateway`) so
no pin/delete/copy/retention command can be dispatched from the UI process.

Single-instance/focus-existing is the *launcher's* job (it asks its own IPC
server whether a child is already connected before ever spawning one), so
this module only ever connects out. IPC reader-thread callbacks are marshalled
onto the Qt thread via queued signal emissions; the focus ack stays a
blocking invoke so it keeps proving the Qt loop is actually alive.
"""

import logging
import os
import sys

from PySide6.QtCore import QObject, QTimer, Signal

from history_ui import cli
from history_ui.client import HistoryIpcClient, credentials_from_environment
from history_ui.controller import HistoryController, ReadOnlyHistoryGateway
from history_ui.window import HistoryWindow


class _IpcEventBridge(QObject):
    """Reader-thread -> Qt-thread bridge. Emitting a signal from the IPC
    reader thread with the window (Qt thread) as receiver yields a queued
    connection, so the slot runs on the Qt thread."""

    event_received = Signal(str, object, bool)


def run(argv):
    """Run the history window, connecting to the authenticated IPC server the
    launcher started. Without valid pipe/token credentials in the environment
    (a bare manual invocation, or history disabled/unavailable at startup),
    the window still opens -- a user gets an explanatory placeholder rather
    than a silent failure -- but has no data connection."""
    options = cli.parse_options(argv)

    from PySide6.QtWidgets import QApplication

    from history_ui import theme

    application = QApplication([sys.argv[0] if sys.argv else "ClipCascade"])
    application.setApplicationName("ClipCascade history")
    application.setQuitOnLastWindowClosed(True)
    application.setStyleSheet(theme.build_stylesheet())

    measurements = {
        "mode": "history-ui",
        "outcome": "opened",
        "pid": os.getpid(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "qt_version": _qt_version(),
        "process_start_to_qapplication_s": cli.elapsed_since_process_start(),
    }

    def on_first_paint():
        measurements["process_start_to_first_paint_s"] = cli.elapsed_since_process_start()
        cli.write_report(options.report, measurements)
        if options.probe:
            QTimer.singleShot(0, application.quit)

    window = HistoryWindow(on_first_paint=on_first_paint)
    client = _connect_client(window, application)
    measurements["ipc_connected"] = client is not None

    window.present()
    if options.hold_seconds > 0:
        QTimer.singleShot(int(options.hold_seconds * 1000), application.quit)

    exit_code = application.exec()
    if client is not None:
        client.close()
    measurements["focus_requests"] = window.focus_requests
    measurements["process_start_to_exit_s"] = cli.elapsed_since_process_start()
    cli.write_report(options.report, measurements)
    return exit_code


def _connect_client(window, application):
    """Best-effort: any failure to reach the main process must still let the
    window open (it just has no data yet), never abort the child."""
    pipe_name, token = credentials_from_environment()
    if pipe_name is None:
        return None

    bridge = _IpcEventBridge()
    bridge.event_received.connect(window.handle_ipc_event)

    def on_event(name, data, gap):
        bridge.event_received.emit(name, data or {}, bool(gap))

    def on_focus():
        _invoke_blocking(window, "handle_focus_request")

    def on_disconnected():
        logging.warning(
            "History IPC: disconnected from the main process; closing history window"
        )
        QTimer.singleShot(0, application.quit)

    client = HistoryIpcClient(
        pipe_name, token, on_event=on_event, on_focus=on_focus, on_disconnected=on_disconnected
    )
    try:
        if not client.connect():
            return None
    except Exception:
        logging.exception("History IPC: failed to connect to the main process")
        return None

    gateway = ReadOnlyHistoryGateway(client)
    controller = HistoryController(gateway, parent=window)
    window.attach_controller(controller)
    controller.start()
    return client


def _invoke_blocking(window, slot_name):
    """Run `slot_name` on the Qt (main) thread and block the caller (the IPC
    reader thread) until it actually ran. This is what makes a focus-window
    ack meaningful evidence of responsiveness: a frozen Qt event loop makes
    this call hang too, so the main process's bounded ack-wait correctly
    times out and treats the child as unresponsive rather than merely
    "the pipe is still open"."""
    from PySide6.QtCore import QMetaObject, Qt

    QMetaObject.invokeMethod(window, slot_name, Qt.ConnectionType.BlockingQueuedConnection)


def _qt_version():
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        return {"pyside6": pyside_version, "qt": qVersion()}
    except Exception:
        return None
