"""On-demand Windows clipboard-history user interface.

The history window runs in its own short-lived child process so that Qt never
shares an event loop with the existing Tkinter login and pystray tray.

This package currently contains only the process bootstrap needed to measure
the packaging cost of PySide6 (ticket T0). The entry list, detail panes and the
authenticated command channel arrive in later tickets.
"""
