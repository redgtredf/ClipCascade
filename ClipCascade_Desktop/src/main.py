#!/usr/bin/env python3

# ClipCascade - A seamless clipboard syncing utility
# Repository: https://github.com/Sathvik-Rao/ClipCascade
#
# Author: Sathvik Rao Poladi
# License: GPL-3.0
#
# This script serves as the entry point for the ClipCascade application,
# initializing and running the core application logic.

import sys

from core.application import Application

# Mirrors history_ui.cli; kept here so a normal launch never imports that
# package (and therefore never pays for Qt). tests/test_history_ui_cli.py
# fails if the two drift apart.
CHILD_MODE_FLAGS = ("--history-ui", "--startup-probe")
EXIT_CHILD_MODE_UNAVAILABLE = 3


def dispatch_child_mode(argv):
    """Return an exit code when argv selects a child mode, else None.

    The history window and the packaging probes run in their own process; the
    normal clipboard application must keep starting even when that support is
    missing from the build.
    """
    if not any(flag in argv for flag in CHILD_MODE_FLAGS):
        return None
    try:
        from history_ui.cli import dispatch
    except Exception as error:
        print(
            f"ClipCascade: history child mode unavailable: {error}",
            file=sys.stderr,
        )
        return EXIT_CHILD_MODE_UNAVAILABLE
    return dispatch(argv)


class Main:
    def __init__(self):
        exit_code = dispatch_child_mode(sys.argv[1:])
        if exit_code is not None:
            sys.exit(exit_code)
        Application().run()

def main():
    Main()

if __name__ == "__main__":
    main()
