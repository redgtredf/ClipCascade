"""QApplication bootstrap for the on-demand history child process.

Started as ``ClipCascade.exe --history-ui``. Only one window may exist per
Windows user: a second launch hands the request to the running window over the
focus channel and exits without ever creating a QApplication.
"""

import os
import sys

from history_ui import channel, cli
from history_ui.window import HistoryWindow


class FocusServer:
    """Answers focus/ping requests for the running window.

    Keeps its own reference to every live connection; Qt would otherwise
    collect the socket while the request is still being read.
    """

    def __init__(self, window):
        from PySide6.QtNetwork import QLocalServer

        self._window = window
        self._connections = []
        self._server = QLocalServer()
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)

    def listen(self, name):
        return self._server.listen(name)

    def close(self):
        self._server.close()

    def _accept(self):
        connection = self._server.nextPendingConnection()
        if connection is None:
            return
        self._connections.append(connection)
        connection.disconnected.connect(lambda: self._drop(connection))
        connection.readyRead.connect(lambda: self._read(connection))
        self._write(connection, {"pid": os.getpid()})

    def _drop(self, connection):
        if connection in self._connections:
            self._connections.remove(connection)
        connection.deleteLater()

    def _read(self, connection):
        import json

        payload = bytes(connection.readAll().data())
        for line in payload.split(b"\n"):
            if not line.strip():
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._write(connection, {"ok": False, "error": "malformed request"})
                continue
            self._write(connection, self._handle(request))

    def _handle(self, request):
        action = request.get("action") if isinstance(request, dict) else None
        if action == channel.ACTION_FOCUS:
            self._window.handle_focus_request()
            return {"ok": True, "action": action, "pid": os.getpid()}
        if action == channel.ACTION_PING:
            return {"ok": True, "action": action, "pid": os.getpid()}
        return {"ok": False, "error": "unknown action"}

    def _write(self, connection, payload):
        import json

        connection.write((json.dumps(payload) + "\n").encode("utf-8"))
        connection.flush()


def run(argv):
    """Run the history window, or focus the one that is already open."""
    options = cli.parse_options(argv)

    # Cheapest path first: an existing window is raised without starting Qt.
    if channel.request_focus():
        cli.write_report(
            options.report,
            {
                "mode": "history-ui",
                "outcome": "focused-existing",
                "pid": os.getpid(),
                "process_start_to_focus_s": cli.elapsed_since_process_start(),
            },
        )
        return cli.EXIT_OK

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    application = QApplication([sys.argv[0] if sys.argv else "ClipCascade"])
    application.setApplicationName("ClipCascade history")
    application.setQuitOnLastWindowClosed(True)

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
    server = FocusServer(window)
    if not server.listen(channel.pipe_name()):
        # Lost a start-up race with another child; let the winner take focus.
        if channel.request_focus():
            measurements["outcome"] = "focused-existing"
            cli.write_report(options.report, measurements)
            return cli.EXIT_OK
        measurements["outcome"] = "focus-failed"
        cli.write_report(options.report, measurements)
        return cli.EXIT_FOCUS_FAILED

    window.present()
    if options.hold_seconds > 0:
        QTimer.singleShot(int(options.hold_seconds * 1000), application.quit)

    exit_code = application.exec()
    measurements["focus_requests"] = window.focus_requests
    measurements["process_start_to_exit_s"] = cli.elapsed_since_process_start()
    cli.write_report(options.report, measurements)
    server.close()
    return exit_code


def _qt_version():
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        return {"pyside6": pyside_version, "qt": qVersion()}
    except Exception:
        return None
