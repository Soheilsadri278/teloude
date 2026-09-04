# Teloude — Personal Telegram Backup (Windows)

Teloude backs up local files and folders to the user's **own Telegram account**
via MTProto (Telethon). Each storage is a private Telegram forum supergroup;
the local SQLite database is the authoritative folder index.

> Status: v1 implementation complete (all `PROJECT_SPEC.md` phases 0–9).
> Target platform: Windows 10/11, 64-bit.

## Quickstart (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"        ^ or: pip install PySide6 telethon pydantic pillow pytest
set TELOUDE_API_ID=123456
set TELOUDE_API_HASH=0123456789abcdef0123456789abcdef
python -m teloude.main
```

First run opens the sign-in wizard: phone number → Telegram login code →
2FA password (if enabled). The session is stored under
`%APPDATA%\Teloude\sessions` and encrypted at rest with Windows DPAPI
whenever the app is closed.

Useful flags:

```text
python -m teloude.main --offline        # local fakes, no network (dev smoke test)
python -m teloude.main --data-dir DIR   # override the data directory
python -m teloude.main --minimized       # start in the system tray (autostart)
```

## Tests

```text
python -m pytest -q
```

All tests run offline (scripted Telegram fakes, temporary databases).
UI smoke tests use the offscreen Qt platform; on minimal Linux containers run
`scripts/ensure_qt_libs.sh` first (Windows and desktop Linux need nothing).

## Packaging

```text
pip install pyinstaller
pyinstaller teloude.spec          # -> dist\Teloude\Teloude.exe
iscc installer\teloude.iss       # -> installer\Output\Teloude-Setup-0.1.0.exe
```

Set `TELOUDE_API_ID` / `TELOUDE_API_HASH` at build time so the packaged app
ships Teloude's own registered Telegram API credentials (spec §11).
Never commit real credentials. Uninstall keeps `%APPDATA%\Teloude`
(database/logs) so cloud metadata is never destroyed implicitly.

## Security model

- Telegram session files: DPAPI-protected at rest (`SecureSessionStore`
  locks on exit/settings action, unlocks silently at startup). On non-Windows
  dev machines an explicit plaintext fallback is used and **labeled as such** —
  never presented as encryption.
- Secrets (codes, passwords, API keys) are never logged; Telegram RPC errors
  are mapped to actionable messages before reaching the UI or logs.
- Restore cannot escape its destination (traversal guard); cloud deletion
  needs explicit confirmation and never touches local files.

## Layout

```text
teloude/  core/           backup / restore / transfers / duplicates / search / preview
          application/    Qt-free services (auth, storage, backup, restore, search, settings)
          infrastructure/ telegram (MTProto gateway + single-loop bridge)
                          database / repositories / security (DPAPI) / config
          ui/             PySide6 views + dialogs + tray + composition root (ui/app.py)
tests/    offline unit + integration + headless UI smoke tests
installer/  teloude.iss   teloude.spec   assets/
```

See `PROJECT_SPEC.md` (product requirements) and `AGENTS.md` (working rules).
