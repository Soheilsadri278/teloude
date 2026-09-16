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

The full suite runs offline (scripted Telegram fakes, temporary databases, and a
scripted MTProto server that speaks real Telethon request objects — no account,
no network). Windows and desktop Linux need nothing extra:

```text
python -m pytest -q
```

On a minimal Linux container, stage Qt's system libraries once:

```text
sh scripts/ensure_qt_libs.sh
export LD_LIBRARY_PATH=/tmp/syslibs/sysroot/usr/lib/x86_64-linux-gnu
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Lint gate (errors only):

```text
python -m ruff check teloude/
```

Before shipping to a real account, walk `docs/live_acceptance_checklist.md`
(the parts that cannot be automated here). Release notes: `docs/CHANGELOG.md`.

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

## Known limits

- **Windows path length.** Windows needs long-path support enabled (Windows 10
  1607+ with `LongPathsEnabled=1`, or a manifest) before any program can open
  paths beyond ~260 characters. Teloude never hides this: overlong paths are
  reported as failures and the rest of the run continues.
- **Upload size** follows the signed-in account tier, read at runtime from
  Telegram (free ~2 GiB, Premium ~4 GiB per file) - nothing is hard-coded.
- **Unreadable sources** (permission denied, file locked by another program) are
  skipped and listed as failures. Source files are never modified or deleted.
- **One backup and one restore at a time.** The speed limit is shared and
  enforced per chunk, so a short burst can briefly exceed the configured rate.
- **Restore keeps local files**: it never overwrites without an explicit
  choice, and never deletes anything that was not created by the restore.

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

See `PROJECT_SPEC.md` (product requirements), `AGENTS.md` (working rules),
`docs/live_acceptance_checklist.md` (manual test on a real account) and
`docs/CHANGELOG.md`.
