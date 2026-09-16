# Teloude — Personal Telegram Backup (Windows)

Teloude backs up local files and folders to the user's **own Telegram account**
via MTProto (Telethon). Each storage is a private Telegram forum supergroup;
the local SQLite database is the authoritative folder index.

> Status: v1 implementation complete (all `PROJECT_SPEC.md` phases 0–9).
> Target platform: Windows 10/11, 64-bit.

## Install (Windows, no Python needed)

Download **`Teloude-Setup-0.1.0.exe`** and double-click it. Teloude installs per
user (no administrator prompt), appears in the Start Menu, and runs like any
other Windows application - no Python, no pip, no compiler, no terminal. The
installer offers an optional desktop shortcut and an optional "start with
Windows (minimized to tray)". Uninstalling removes the program and never touches
your data or your Telegram session.

Building that installer from source is one command - see
[`docs/installer_build_and_test.md`](docs/installer_build_and_test.md):

```powershell
$env:TELOUDE_API_ID = "<api id>"; $env:TELOUDE_API_HASH = "<api hash>"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SmokeTest
# -> installer\Output\Teloude-Setup-0.1.0.exe
```

## Run from source (developers)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"        ^ or: pip install PySide6 telethon pydantic pillow pytest ruff
set TELOUDE_API_ID=123456
set TELOUDE_API_HASH=0123456789abcdef0123456789abcdef
python -m teloude.main
```

On Python 3.14 or newer, PySide6 must be 6.10.1 or later - the first release
with 3.14 wheels (`pip install -e .` resolves that automatically; `pip install
PySide6` on an older pin is what leaves the GUI dependency missing).

First run opens the sign-in wizard: phone number → Telegram login code →
2FA password (if enabled). The session is stored under
`%APPDATA%\Teloude\sessions` and encrypted at rest with Windows DPAPI
whenever the app is closed. Later launches reopen that session silently: the
sign-in wizard only appears when there is no usable session left (first run,
signed out, revoked, or unreadable).

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
no network). Windows and desktop Linux need nothing extra.

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
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1   # the one command
```

It runs PyInstaller (`teloude.spec` -> `dist\Teloude\Teloude.exe`, a bundle
that carries its own Python runtime) and then Inno Setup
(`installer\teloude.iss` -> `installer\Output\Teloude-Setup-0.1.0.exe`), and
prints the installer path, size and SHA-256. Both steps are Windows-only:
PyInstaller never cross-compiles and ISCC is a Windows tool. Full prerequisites,
options, the installer's behaviour (per-user install, shortcuts, optional
autostart, upgrade path, uninstall that keeps user data) and the Windows
acceptance checklist: [`docs/installer_build_and_test.md`](docs/installer_build_and_test.md).

The release build bakes `TELOUDE_API_ID` / `TELOUDE_API_HASH` into the bundle
(spec section 11: Teloude uses its own registered credentials and does not ask
the user for them), because an installed application started from the Start Menu
has no environment to read. The build script writes them to a gitignored
`installer\build_credentials.json`, PyInstaller ships that file inside the
bundle, and the file is deleted again afterwards - values never enter the
repository, the history or the logs. The environment still wins at runtime, so
development, tests and support overrides are unchanged; a build without
credentials starts only when both variables are set, and prints exactly what is
missing (exit code 2). Uninstall keeps `%APPDATA%\Teloude`
(database/logs/session) so cloud metadata is never destroyed implicitly.

## Security model

- Telegram session files: DPAPI-protected at rest (`SecureSessionStore`
  locks on exit/settings action, unlocks silently at startup). On non-Windows
  dev machines an explicit plaintext fallback is used and **labeled as such** —
  never presented as encryption.
- Telegram API credentials come from the environment at runtime: never
  hard-coded, never written to the database, never logged (an invalid
  `TELOUDE_API_ID` logs the variable name only, never the value).
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
- **Pause and Resume show what is really happening.** While a run is live the
  status line names its confirmed state (`Uploading`/`Restoring`, `Pausing…`,
  `Paused`, `Resuming…`) and the buttons only offer commands that change
  something: Pause while the transfer runs, Resume while a pause is pending or
  in effect. Both the Backup and the Restore page behave the same way.
- **Restore rebuilds the folder you selected.** Restoring a storage into
  `D:\Restored` gives `D:\Restored\<selected folder>\...` with the whole
  hierarchy, never a flattened top level. Each root folder backed up into one
  storage also gets its own forum topic (`<storage> / <folder>`), created on
  its first backup and reused afterwards - two folders never share a topic.
- **Restore keeps local files**: it never overwrites without an explicit
  choice, and never deletes anything that was not created by the restore.
- **Large storages**: the restore page builds one row per folder and loads a
  folder's files when you expand it; ticking a folder selects everything inside
  it, rendered or not. Search is capped at 200 results per query.
- **Bounded local state**: transfer history keeps the newest 200 finished rows,
  previews are capped at 200 thumbnails / 50 MB, and logs rotate at 2 MB x 3.
- **Transient Telegram limits** (flood waits) are retried with the wait time
  Telegram reports; the current attempt and its reason are logged.
- **Backups are incremental**: an unchanged file is skipped (nothing is sent to
  Telegram). Unchanged is decided in two tiers (spec §21): file size and
  modification time first (a file whose size *and* mtime are untouched is not
  re-read), then SHA-256 whenever either changed. Editing a file in place
  changes its mtime, so the content is hashed and the file re-uploaded; the
  message it replaces is deleted only after the new copy is verified. Tick
  *Verify file contents again (slower)* on the Backup page to force a full
  SHA-256 of every file. Local files are never touched either way.
- **Expired sessions are recoverable**: if Telegram invalidates the session
  (signed in elsewhere, revoked, account deactivated) the run stops with a
  failed transfer row and the app offers to sign in again, then refreshes
  itself — no restart, no raw RPC error.
- **Broken storage links are repairable**: if a storage's group is deleted or
  becomes inaccessible, *Repair link…* on the Storages page creates a fresh
  private forum group after an explicit confirmation, keeps the local index,
  and the next backup re-uploads to the new group.
- **Deleted messages** fail only that file ("Run a backup again for this
  file"); the rest of the restore continues.

## Layout

```text
teloude/  core/           backup / restore / transfers / duplicates / search / preview
          application/    Qt-free services (auth, storage, backup, restore, search, settings)
          infrastructure/ telegram (MTProto gateway + single-loop bridge)
                          database / repositories / security (DPAPI) / config
          ui/             PySide6 views + dialogs + tray + composition root (ui/app.py)
          tests/          offline unit + integration + headless UI smoke tests
teloude.spec      PyInstaller spec (builds dist/Teloude with its own Python runtime)
installer/        teloude.iss — Inno Setup script (per-user install, keeps user data)
assets/           application icon
scripts/          build_windows.ps1/.bat — one-command Windows release build
                  ensure_qt_libs.sh — stages Qt system libraries on a bare Linux box
docs/             changelog, live acceptance checklist, v1 audit + final report
PROJECT_SPEC.md   product requirements (source of truth)
AGENTS.md         working rules for contributors and agents
```

See `PROJECT_SPEC.md` (product requirements), `AGENTS.md` (working rules),
`docs/live_acceptance_checklist.md` (manual test on a real account),
`docs/CHANGELOG.md` (release notes) and `docs/FINAL_REPORT.md` (v1 audit and
final report).
