# teloude.spec — PyInstaller build for Teloude (run on Windows).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#
# ...or by hand:
#
#   pip install pyinstaller
#   pyinstaller teloude.spec
#   iscc installer\teloude.iss
#
# Output: dist/Teloude/Teloude.exe (windowed, no console) — a self-contained
# directory that runs on a machine without Python, pip or a compiler.
# Telethon generates TL types dynamically, so all of its submodules are
# collected explicitly. No API keys, sessions, or user data are bundled, with
# one deliberate exception: installer/build_credentials.json is included when it
# exists, because spec section 11 requires the packaged application to ship
# Teloude's own registered credentials rather than asking the user for them.
# That file is gitignored and written by scripts/build_windows.ps1 from the
# builder's environment - it never enters the repository or its history.
# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

telethon_hidden = collect_submodules("telethon")

# Credentials baked in by the release build (gitignored; absent in a source
# checkout, in which case the application reads TELOUDE_API_ID/API_HASH from the
# environment as before).
build_credentials = os.path.join("installer", "build_credentials.json")
datas = [("assets/icon.png", "assets"), ("assets/icon.ico", "assets")]
if os.path.isfile(build_credentials):
    datas.append((build_credentials, "."))
else:
    print(
        "teloude.spec: no installer/build_credentials.json - this bundle will "
        "need TELOUDE_API_ID / TELOUDE_API_HASH in the environment at startup."
    )

a = Analysis(
    ["teloude/main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
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
