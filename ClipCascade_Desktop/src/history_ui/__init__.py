"""On-demand Windows clipboard-history user interface.

The history window runs in its own short-lived child process so that Qt never
shares an event loop with the existing Tkinter login and pystray tray.

This package contains the read-only history window (theme, virtualised entry
model, detail panes, filters and search) plus the process bootstrap and the
authenticated IPC client. The UI process never sees SQLite, DPAPI keys or
history files: every byte it displays arrives already decrypted over
`history_ui.client.HistoryIpcClient`, and `history_ui.controller`'s
read-only gateway exposes only query/detail actions -- mutating commands
(copy, open, download, pin, delete, clear, retention) arrive in ticket T6.
"""
