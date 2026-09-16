# Changelog

Notable changes, newest first. Versions are milestone commits, not releases.

## 2026-09-16 — resilience fixes found by exercising the real gateways

The engine tests used fakes that ignored parts and progress offsets, so a whole
class of resume bugs was invisible. A scripted MTProto server that speaks real
Telethon request objects (`teloude/tests/test_real_wiring.py`) now drives the
production stack — login → storage → backup → restore → adopt → delete — and it
exposed four defects, all fixed here:

1. **Real-mode wiring was incoherent.** `TelethonStorageGateway` was constructed
   with `get_me=` instead of its required `list_dialogs=`, and `TelethonAuth`
   received a wrapper lacking `send_code_request`/`sign_in`. Live sign-in and
   storage creation raised `AttributeError`/`TypeError`. The composition root now
   hands the raw Telethon client to the gateways and supplies
   `telethon_list_dialogs(...)`; the adapter class is gone. Asking for gateway
   work before sign-in now returns a clear "sign in first" error instead of a
   raw `KeyError`.
2. **Paused uploads produced broken documents.** Telegram stores upload parts
   under the file id that was used to write them. On resume the engine passed
   `start_part > 0` to a fresh `upload()` call, which allocated a new file id —
   so the resumed attempt held only the later parts, and the md5 it sent covered
   just those. The gateway now refuses an unanchored resume (it restarts
   instead), seeds the md5 from the skipped prefix, and the engine carries the
   original file id through pause/resume and restarts cleanly after errors.
3. **Progress double-counted on retries.** A resumed attempt added its absolute
   byte count to the stale value in the transfer row, so the UI could show 200%
   for a file. Progress is now reported and stored as absolute file bytes, and
   restarting a file resets its checkpoint.
4. **Crash recovery could crash.** `recover_pending()` used the strict state
   machine to fail stale rows; a row left `paused` by a killed process made it
   raise `Illegal transition paused -> failed` at startup. Recovery now uses the
   crash-recovery path (`set_status_quiet`) for those rows.

Also added: a Telethon contract test that resolves every Telethon import in the
gateway modules against the installed version and checks the serialization of
requests whose fields are set by name (a library upgrade now fails in CI rather
than during a live backup), plus `docs/live_acceptance_checklist.md` for the
manual soak on a real account.

Also fixed: `AppConfig.get_data_dir()` called an undefined helper, so launching
the app (or the installed build) *without* `--data-dir` — the normal shortcut
case — raised `NameError` at startup; the default data/session directories now
resolve for both the Windows and POSIX layouts. Dead imports left over from
earlier phases were removed, and `python -m ruff check teloude/` (errors only)
is now a documented gate.

Test count: 206 (1 Windows-only skip).

## Earlier milestones

- v1 implementation across spec phases 0–9: auth with code + 2FA, forum-supergroup
  storage with flat topics, chunked upload/download with pause/resume/retry/crash
  recovery, SHA-256 duplicate handling, verified restore with collision handling,
  SQLite schema + migrations, full UI (onboarding, dashboard, transfers, restore,
  search, preview, settings, tray), enforced speed limits.
- Packaging: PyInstaller spec + Inno Setup script, app icon, autostart via
  `--minimized`, data preserved on uninstall.
- Data-directory fix: a relative `database_path` is now anchored under
  `--data-dir` instead of the working directory.
