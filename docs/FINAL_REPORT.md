# Teloude — v1 final audit and report

Date: 2026-09-16 · HEAD when the audit was written: `c21af90` (see Part C for the
GitHub-delivery addendum) · Platform audited: Linux
container (offscreen Qt); target platform: Windows 10/11 x64.

This document is the end-of-task deliverable: first the audit of the v1
definition of done and of every specification area, then the report
(implementation, architecture, features, Telegram, database, UI, security,
testing, packaging, git, limitations, verdict).

---

## Part A — Audit

### A.1 Spec §47 definition of done (16 user-visible outcomes)

| # | Item | Evidence | Status |
|---|------|----------|--------|
| 1 | Install Teloude on Windows | `teloude.spec` (PyInstaller) + `installer/teloude.iss` (Inno Setup, per-user, optional desktop/autostart tasks, uninstall keeps user data) | Artifact complete; not compiled here (needs Windows + Inno Setup) |
| 2 | Sign in with Telegram | `infrastructure/telegram/auth.py`, `ui/auth_dialog.py`, phone → code → 2FA (`SessionPasswordNeededError` branch); `tests/test_telegram_auth.py`, `test_session_auth.py` | Tested (42 auth-focused tests) |
| 3 | Create a Teloude storage | `infrastructure/telegram/storage.py` (private supergroup → forum → root topic), `application/services.py::StorageService.create_storage`; `test_telegram_storage.py`, `test_services.py` | Tested |
| 4 | Select a local folder | `ui/views/backup_view.py` (Browse + validation of existence/dir-ness), `core/scanner.py`; `test_ui_flows.py`, `test_scanner.py` | Tested |
| 5 | Back up arbitrary files | `core/backup.py` (plan → per-file upload → topic per folder), chunked engine in `infrastructure/telegram/files.py`; `test_engines.py`, `test_first_run_journey.py` | Tested |
| 6 | See progress | `backup_progress`/`restore_progress` bus events → `ui/views/backup_view.py`, `transfers_view.py` (coalesced); `test_ui.py::test_backup_flow_to_completion` | Tested |
| 7 | Pause/resume | `core/control.py` + `TransferRegistry` states; `test_engines.py` (pause/resume round trip), `test_ui.py` | Tested |
| 8 | Recover after application restart | `recover_pending()` at startup, `set_status_quiet` for crash-orphaned rows; `test_core_state.py`, `test_engines.py` | Tested |
| 9 | Recover after temporary network loss | `core/errors.py::is_network_error` + bounded retry with backoff in both engines, flood-wait handling; `test_engines.py`, `test_recovery_flows.py` | Tested |
| 10 | Detect duplicates | SHA-256 in `core/scanner.py` + `core/duplicates.py` (Skip / Upload Again / Cancel + Apply‑to‑all), `ui/views/backup_view.py` ask dialog | Tested |
| 11 | Search backed-up files | `core/search.py` + `ui/views/search_view.py`; `test_search_preview.py` | Tested |
| 12 | Restore files/folders | `core/restore.py` (per-file and whole-folder, collision policy, md5 verification), `ui/views/restore_view.py`; `test_engines.py`, `test_scale.py`, `test_first_run_journey.py` | Tested |
| 13 | Preview supported media | `core/preview.py` (Pillow thumbnails, caps) + `ui/views/preview.py`; `test_search_preview.py` | Tested |
| 14 | Manage multiple storages | `StorageRepository` + `ui/views/storages.py` (create/adopt/refresh/delete/repair); `test_services.py`, `test_recovery_flows.py` | Tested |
| 15 | Use the application from the system tray | `ui/tray.py`, `--minimized`, safe shutdown (session locked, DB closed); `test_ui_flows.py` | Tested (offscreen; no real tray on this container) |
| 16 | Uninstall Teloude safely | `installer/teloude.iss`: uninstall removes program files, shortcuts and the autostart value; `%APPDATA%\Teloude` (index, logs, previews, session) is intentionally kept, and nothing on Telegram is touched | Artifact complete; not executed here |

### A.2 Specification areas 1–50

| Area | Covered by | Status |
|------|-----------|--------|
| Purpose, non-goals (§1, §48) | No sync, no scheduler, no auto local deletion/modification, MTProto only (no Bot API anywhere — verified by grep), forum topics kept flat | Respected |
| Architecture, engineering rules (§2, §49) | 4 layers: `core` (Qt-free domain), `application` (services), `infrastructure` (SQLite, Telegram, security), `ui`; Telegram behind `ITelegram*Gateway` interfaces | Respected |
| Data model, storage layout (§3–§8) | SQLite is authoritative; one flat topic per folder; folders mirrored, never nested topics | Implemented |
| Auth, session handling (§9–§13) | Telethon + `SecureSessionStore` (DPAPI on Windows, labeled plaintext fallback elsewhere), sign-out locks/labels the file; expiry → re-auth flow | Implemented |
| Storage lifecycle (§14–§18) | Create, adopt existing, refresh, delete, repair; DB-authoritative hierarchy | Implemented |
| Backup engine (§19–§22) | Scan → hash → plan (unchanged / superseded / new) → chunked upload with pause, resume, retry, crash recovery; duplicate dialog incl. apply-to-all | Implemented |
| Restore (§23–§27) | Per-file/folder restore, collision handling (ask / overwrite / keep both / skip), post-download verification, traversal guard | Implemented |
| Search & preview (§28–§31) | Indexed search with caps, preview thumbnails with size/count caps | Implemented |
| Transfers & UI (§32–§36) | 7 pages (Overview, Storages, Backup, Transfers, Restore, Search, Settings), tray, actionable history rows, coalesced refresh | Implemented |
| Settings (§37) | Speed limit (MB/s), retry/timing, data dir, session actions; validated and persisted | Implemented |
| Security & privacy (§38–§41) | Secrets never logged, session at rest, restore cannot escape destination, cloud deletion always confirmed, no local deletion ever | Implemented |
| Performance & scale (§42–§44) | 50k-file tree: lazy rows (0.85 s), transfers refresh coalescing, history capped at 200 rows, preview caps, streaming SHA-256 | Implemented |
| Errors & recovery (§45–§46) | Every Telegram error mapped to a user-facing class; retryable vs fatal separated; three recovery flows (session expired, storage unavailable, remote item missing) | Implemented |
| Definition of done (§47) | Table A.1 | 14 verified by tests, 2 verified as artifacts |

### A.3 Repository hygiene

- `python -m pytest -q` → **281 passed, 1 skipped** (the skip is the Windows-only
  long-path guard test). `python -m ruff check teloude/` → clean.
- Working tree clean; the only untracked file is a user-supplied patch
  (`phase-1.3-session-auth.patch`) that was deliberately left alone.
- No secrets in tracked files: no session files, no `.db`, no real API id/hash or
  phone numbers (only clearly fake `+10000000000` in the scripted-server test and
  the placeholder values in the README quickstart).
- Never pushed anywhere; all work is local commits.

---

## Part B — Report

### 1. Implementation

12 958 lines of Python: 7 951 in `teloude/` (production) and 5 007 in
`teloude/tests/`. Entry point `python -m teloude.main`; flags `--offline`
(local fakes, no network), `--data-dir DIR`, `--minimized`. The application is a
single-process PySide6 app where a Qt-free core does the work and Qt only
renders state.

### 2. Architecture

```text
ui/            PySide6 views, dialogs, tray, worker/bridge plumbing
application/   services.py — Qt-free orchestration, background threads, bus events
core/          scanner, backup, restore, transfers, duplicates, topics, search,
               preview, speed limiter, control, errors
infrastructure/ database.py + repositories.py (SQLite), telegram/ (bridges,
               auth, storage, files, fakes), security/ (DPAPI), logging.py
```

Rules that held throughout: the UI never calls Telegram directly (it goes through
services → gateways); every long operation runs off the UI thread and reports
through a signal bus; the SQLite index — not Telegram — decides what exists; the
Telegram layer is replaceable (scripted fakes in tests, Telethon in production).

### 3. Features

- Onboarding wizard (phone → code → 2FA) with session persistence and locking.
- Storages: create a private forum supergroup, adopt an existing one, refresh,
  delete, and **repair** a broken link into a fresh group.
- Incremental backup with two-tier unchanged detection, duplicate dialog
  (Skip / Upload Again / Cancel / apply-to-all), per-folder topics, pause,
  resume, cancel, retry, crash recovery, verification before replacing a file.
- Transfers page: live queue, progress/speed/ETA, per-row Pause/Resume/Retry/
  Cancel, bounded history that stays actionable.
- Restore: single file, multi-selection, whole folders, collision handling, and
  md5 verification of every downloaded file.
- Search across all storages with a preview pane (images/pdf/text, thumbnails).
- Settings: speed limit actually enforced per chunk, retry policy, data
  directory, session lock/sign-out; tray icon with safe shutdown and autostart.
- Recovery flows: expired session → re-authentication prompt and full refresh;
  inaccessible storage → repair link; deleted message → that one file fails and
  the run continues.

### 4. Telegram integration

MTProto via Telethon only — no Bot API code exists in the repository. Uploads are
chunked with real part semantics (a paused upload keeps its file id, so resumed
parts land on the same document; the md5 covers the skipped prefix), and the
gateway refuses unanchored resumes instead of producing a corrupt document. All
RPC errors are mapped to dedicated user-facing classes in
`infrastructure/telegram/exceptions.py` (`SessionExpiredError`,
`StorageUnavailableError`, `RemoteItemMissingError`, `RateLimitExceeded`,
`ConnectionStateError`) by class-name MRO (so a Telethon upgrade
cannot silently downgrade them): session expired, storage unavailable, remote
item missing, connection state, rate limit (with the server's wait time).
`tests/test_telethon_contract.py` resolves every Telethon import used by the
gateways against the installed version, and `tests/test_real_wiring.py` drives
the production stack through a scripted MTProto server that speaks real request
objects.

### 5. Database

SQLite, WAL, one connection per thread with an RLock, idempotent close.
`SCHEMA_VERSION = 2` with a `schema_migrations` ledger and a `MIGRATIONS` list
(`_migrate_v1`, `_migrate_v2`) applied in order. Tables: `storages`, `folders`,
`files` (path, size, mtime, SHA-256, fingerprint, backed-up flag, Telegram chat/
message ids), `telegram_messages`, `transfers` (state machine with legal
transitions), `settings`. Startup trims transfer history to the newest 200 rows;
the file index is never trimmed automatically. Backup metadata lives only in the
database plus Telegram — no sidecar files next to the user's data.

### 6. UI

Seven pages plus the auth wizard, duplicate/collision dialogs and the preview
pane. Everything heavy is asynchronous: workers run in threads, results cross to
the UI through a `_Signals` bridge with a carrier registry, and refresh bursts
are coalesced (≤5 rebuilds/second, identical content skipped, selection kept).
Large storages stay responsive: the restore tree builds one row per folder and
materialises files on expand (50 000 files: 0.85 s vs 4.17 s before). Modal
dialogs are injectable, which is what makes the UI testable offscreen.

### 7. Security

- The session file is DPAPI-encrypted on Windows and locked on exit/settings
  action; on non-Windows dev machines a plaintext fallback is used and
  **labeled as such** — it is never presented as encryption.
- Secrets (login codes, 2FA passwords, API keys) are never logged; Telegram RPC
  errors are mapped to actionable text before reaching the UI or the log.
- Restore is confined to its destination (traversal guard) and never overwrites
  without an explicit choice; nothing created by the restore is deleted
  afterwards.
- Cloud deletion (deleting a storage, replacing a changed file's old copy) always
  requires explicit confirmation and is performed only after the replacement is
  verified; local files are never deleted or modified by any code path.
- No credentials are requested or stored by the app; API id/hash come from the
  environment at runtime.

### 8. Testing

Offline only — no account, no network. 282 tests in 22 test modules
(plus `conftest.py`), green in ~43 s: scripted MTProto server (production wiring), Telethon contract checks,
engines (plan/upload/pause/resume/retry/crash recovery/duplicates), recovery
flows (expired session, storage repair, deleted message), boundary conditions
(locked files, read-only destinations, disk full, unicode/long/deep paths),
scale (a 20 000-file storage, a 50 000-file restore tree, bounded state,
  coalescing), database
concurrency and migrations, services, UI flows offscreen, packaging smoke via
`--offline`, first-run journey end to end (sign in → storage → backup →
unchanged re-run → edited file replacement → search → restore), and a bounded-
state check of everything the data directory stores.

### 9. Packaging

`teloude.spec` builds a one-folder PyInstaller distribution (`dist/Teloude/`,
includes the icon, excludes test packages); `python -m PyInstaller --noconfirm
teloude.spec` completed successfully here and the packaged binary was smoke-run
with a throwaway HOME: it started, created `%APPDATA%`-equivalent data
directories (`.teloude/{logs,previews,sessions}` + SQLite database) and ran until
killed by the timeout with no traceback. `installer/teloude.iss` wraps it in Inno
Setup as a per-user install with optional desktop shortcut and autostart, and
leaves user data in place on uninstall. Neither artifact has been exercised on
Windows in this environment.

### 10. Git

Local commits only, never pushed. History since the imported branch:
`f344d68` (spec gaps, packaging, icon, docs) → `9912a23` (database path inside
the data dir) → `fe0ac6c` (ignore build outputs) → `4510fd1` (real-mode wiring +
scripted server) → `015aea4` (resume corruption, startup crash, crash recovery) →
`ddd7982` (worker results, DB serialization, source validation) → `6932a86`
(hostile files and destinations) → `0db3ef3` (scale and rate limits) →
`c21af90` (session recovery, storage repair, incremental backups). Working tree
clean; no remote configured for these commits.

### 11. Limitations and residual risks

- **Windows-only items cannot be executed here**: DPAPI encryption, the tray,
  long-path behaviour, installer install/uninstall. They are implemented and
  reviewed, not demonstrated on Windows.
- **No live Telegram soak test** was performed (no credentials were requested,
  by instruction). `docs/live_acceptance_checklist.md` lists the manual steps to
  run against a real account; the scripted MTProto server covers the same code
  paths but cannot prove Telegram's own behaviour.
- **Unchanged detection is tiered**: size + mtime decide first, SHA-256 when
  either changed. A file edited while keeping both its size *and* its mtime is
  therefore seen as unchanged; the Backup page's *Verify file contents again*
  tick forces a full hash and is the documented remedy.
- **Speed limit granularity**: enforcement is per chunk, so a short burst can
  briefly exceed the configured rate (~one chunk).
- **Path length**: Windows needs long-path support enabled for paths beyond
  ~260 characters; overlong files are reported as failures and the run continues.
- **Upload size** follows the signed-in account tier, read at runtime from
  Telegram; nothing is hard-coded.
- **Restore keeps local files**: it never deletes anything it did not create, so
  a cancelled restore leaves partial output in place.
- **One backup and one restore at a time** — the engines and the speed limiter
  are shared state by design.

### 12. Verdict

Version 1 is implementation-complete against `PROJECT_SPEC.md` phases 0–9 and
§47: all 16 outcomes exist, 14 are proven by the automated suite (282 tests, all
green, offline), and the two Windows-packaging outcomes are delivered as
reviewed artifacts. The repository is clean, secret-free, `ruff`-clean, and
never pushed. What remains before shipping to a real account is Windows
execution — the installer, DPAPI, and the tray — and the live Telegram soak in
`docs/live_acceptance_checklist.md`. Nothing in the code needs a credential to
build, test, or review.

---

## Part C — Post-audit addendum (GitHub delivery)

The audit in Parts A and B describes the code at `c21af90`. Preparing the
repository for GitHub then found one release-blocking defect and a few hygiene
items; all are fixed on top of that commit:

1. **Real mode could not start.** `teloude/ui/app.py` imports `TELEGRAM_API_ID` /
   `TELEGRAM_API_HASH` from `teloude.config`, but those names did not exist, so
   `python -m teloude.main` (without `--offline`) raised
   `ImportError: cannot import name 'TELEGRAM_API_ID'` before any validation —
   the application could never reach Telegram. Every smoke test during the build
   used `--offline`, and the "real wiring works" evidence came from the scripted
   MTProto server driving `build_real` directly, so the defect in the entry point
   in front of that wiring stayed hidden. `teloude/config.py` now defines both
   constants, read at startup from `TELOUDE_API_ID` / `TELOUDE_API_HASH` (never
   hard-coded, never logged, never stored), the startup message states the real
   remedy, and the intended validation runs: with credentials missing the app
   prints the message and exits with code 2. Four regression tests cover the
   import itself, the environment mapping, the no-leak logging rule, and a
   subprocess run of the real entry point.
2. **A real-looking phone number was in the test suite.**
   `teloude/tests/test_session_auth.py` used `+989121234567` (an Iranian mobile
   pattern); it is now the reserved fictional number `+15005550006`. The old
   literal remains inside commit `427849d` — the delivery rules forbid rewriting
   history, so removing it from the past would need an explicit history rewrite
   by the repository owner.
3. **`.gitignore` hardened** for `.venv/`, `*.egg-info/`, `.pytest_cache/`,
   `.ruff_cache/`, `.mypy_cache/`, SQLite sidecars (`*.db-wal`, `*.db-shm`,
   `*.sqlite`), `.env*`, `*.log` and editor directories. Verified that no tracked
   file matches any ignore rule.
4. **README corrections**: the duplicated `python -m pytest -q` block in the
   Tests section, the Layout section (it filed `teloude.spec` under
   `installer/`), the packaging paragraph (it claimed credentials are set at
   *build* time; they are read from the environment at runtime), and a pointer
   to this report.

State after the addendum: **281 passed, 1 skipped**, `ruff` clean, real-mode
startup smoke exits 2 with the actionable message and no `ImportError`,
`--offline` smoke runs with no traceback, PyInstaller rebuild and packaged smoke
re-verified, and no secrets, session files, databases, build outputs or personal
data in the delivered tree.
