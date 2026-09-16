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

## 2026-09-16 — boundary conditions (locked files, hostile destinations, odd names)

Walking the paths users actually hit on real machines:

1. **One unreadable file sank the whole backup.** Hashing happened inside
   planning, so a file locked by another program (or unreadable because of
   permissions) raised straight out of `plan()` and the run reported "failed
   unexpectedly" without uploading anything. Planning now skips such files,
   reports them under the file's own name with an actionable message, and
   continues.
2. **Local failures were reported as network failures.** A read-only destination
   folder produced "Network unreachable" and five pointless retries, because
   every `OSError` was treated as an outage. Failures are now classified
   (`core/errors.py`): permission/disk/path problems fail once with the real
   cause ("permission denied", "no space left on this drive"), while genuine
   connection errors still retry.
3. **Restoring into a file crashed the batch.** A destination path that is a
   file raised a bare `FileExistsError`; it is now refused up front with "…is a
   file, not a folder", and a file blocking a sub-folder is reported per file
   ("'sub' is a file, but a folder is needed there") without touching it.

Covered by 13 new tests: non-ASCII/emoji/180-character names and 25-level-deep
trees round-tripping byte-for-byte, unreadable files and folders, read-only
destinations, disk-full (both pre-checked and mid-write), destinations that are
files, and sources deleted, modified or replaced by a directory between
planning and upload. `README.md` now lists the known limits (Windows long
paths, account tier size caps, chunk-granularity speed limit).

## 2026-09-16 — UI flows and shared-state audit

Exercising the widgets (not just the services) surfaced three more defects:

1. **Background results were dropped.** `run_in_background` handed the work to
   `QThreadPool` and returned a task object that every caller ignored; Qt then
   destroyed the runnable before its queued signal was delivered, so *no*
   callback ever ran. Sign-in never left the "requesting a code" state, search
   left its button disabled with empty results, previews stayed on "Loading
   preview...", and the storage page disabled itself permanently. The runner now
   keeps each task's signal carrier alive until the result lands on the UI
   thread, releasing it afterwards (and logs task failures instead of printing
   tracebacks). Nine widget-level tests cover the wizard (code + 2FA), search,
   preview and storage creation.
2. **The shared SQLite connection was unsynchronized.** Engines write from worker
   threads while the UI reads and writes through the same connection, so a
   second thread's implicit transaction could join (and commit) an in-flight one,
   or roll it back on failure. All access now goes through a re-entrant lock, and
   closing waits for any in-flight transaction. Covered by a deterministic
   serialization test, a six-thread stress test, and a close-while-busy test
   (two of them fail if the lock is removed).
3. **Backup started with an unusable source.** A missing path or a file instead
   of a folder was reported only after a worker thread ran and failed; the view
   now validates the folder up front with a specific message.

Also fixed: `AppConfig.get_data_dir()` called an undefined helper, so launching
the app (or the installed build) *without* `--data-dir` — the normal shortcut
case — raised `NameError` at startup; the default data/session directories now
resolve for both the Windows and POSIX layouts. Dead imports left over from
earlier phases were removed, and `python -m ruff check teloude/` (errors only)
is now a documented gate.

Test count: 236 (1 Windows-only skip).

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
