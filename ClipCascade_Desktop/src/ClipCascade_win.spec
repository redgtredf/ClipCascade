# -*- mode: python ; coding: utf-8 -*-

import os

# The on-demand PySide6 history window is bundled only when the build
# environment opts in, so a baseline build and a history build can be produced
# from the same source tree with the same toolchain.
WITH_HISTORY_UI = os.environ.get("CLIPCASCADE_WITH_HISTORY_UI", "0") == "1"

hiddenimports = ['plyer.platforms.win.notification']

# main.py imports these lazily, inside functions, so name them explicitly
# rather than relying on the analyser walking that far.
hiddenimports += ['history_ui.cli', 'history_ui.channel']

# shiboken6 uses NumPy only when it is importable; keeping NumPy out of the
# bundle keeps the packaged child free of the NumPy ABI mismatch regardless of
# what happens to be installed on the build machine.
excludes = ['numpy']

if WITH_HISTORY_UI:
    hiddenimports += ['history_ui.main', 'history_ui.window', 'history_ui.launcher']
    # Qt modules the history window never imports. PySide6-Essentials ships
    # them, and every one left in raises the archive size and therefore the
    # extraction time of every cold start.
    excludes += [
        'PySide6.Qt3DAnimation',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput',
        'PySide6.Qt3DLogic',
        'PySide6.Qt3DRender',
        'PySide6.QtCharts',
        'PySide6.QtDBus',
        'PySide6.QtDataVisualization',
        'PySide6.QtDesigner',
        'PySide6.QtHelp',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
        'PySide6.QtPrintSupport',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.QtQuickControls2',
        'PySide6.QtQuickWidgets',
        'PySide6.QtSql',
        'PySide6.QtTest',
        'PySide6.QtUiTools',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
    ]
else:
    # A build without the history window must still start, and must still
    # answer --history-ui with a clean "unavailable" exit.
    excludes += ['PySide6', 'shiboken6', 'history_ui.main', 'history_ui.window']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClipCascade',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['../../logo/logo.ico'],
)
