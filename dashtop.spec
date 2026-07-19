# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for dashtop — single-folder bundle with static assets."""

import os
import sys
from pathlib import Path

BASE = Path(SPECPATH)  # directory containing this .spec file

a = Analysis(
    [str(BASE / "server.py")],
    pathex=[str(BASE)],
    binaries=[],
    datas=[
        (str(BASE / "static"), "static"),  # bundle the web frontend
    ],
    hiddenimports=["psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dashtop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # show the console window with the URL banner
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
