# teloude.spec — PyInstaller build for Teloude (run on Windows).
#
#   pip install pyinstaller
#   pyinstaller teloude.spec
#
# Output: dist/Teloude/Teloude.exe (windowed, no console).
# Telethon generates TL types dynamically, so all of its submodules are
# collected explicitly. No API keys, sessions, or user data are bundled.
# NOTE: set TELOUDE_API_ID / TELOUDE_API_HASH at build time so the packaged
# app ships Teloude's own registered Telegram API credentials (PROJECT_SPEC
# section 11). Never commit real credentials to the repository.
# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

telethon_hidden = collect_submodules("telethon")

a = Analysis(
    ["teloude/main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/icon.png", "assets")],
    hiddenimports=telethon_hidden + [
        "telethon.tl.types",
        "telethon.tl.functions",
        "imageio_ffmpeg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest", "mypy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Teloude",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed desktop app: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Teloude",
)
