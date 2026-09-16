# Changelog

Notable changes, newest first. Versions are milestone commits, not releases.

## 2026-09-16 — session recovery, storage repair, and incremental backups

Walking the first-run journey end to end (sign in → create storage → back up →
run again → search → restore) turned up the last three gaps in how Teloude
handles a Telegram-side problem, plus a correctness bug that only shows up on
the second backup:

1. **A file edited since the last backup was uploaded again — and left the old
   copy behind.** The planner never compared the indexed copy with the file on
   disk, so every run re-uploaded everything: doubled Telegram storage, and for
   a changed file the previous message stayed in the topic forever. The plan
   now classifies each file as *unchanged* (size and mtime identical to the
   indexed row — nothing is re-read, and nothing is sent to Telegram — or, when
   either changed, an identical SHA-256: skipped) or *superseded* (changed:
   uploaded, and the copy it replaces is deleted from Telegram **after** the
   new upload is verified with the server-side md5 check). If the replacement
   fails, the old cloud copy is deliberately kept, so a failed run never loses
   data. The UI reports it plainly: `Uploaded 0, skipped 4, failed 0. 4 already
   up to date.` Local files are still never modified or deleted.

   The two tiers are deliberate (spec §21): size + mtime makes a repeat backup
   of an untouched set a pure metadata walk instead of a full re-read, and the
   hash is what decides identity whenever a file looks touched. The trade-off is
   the classic one — a file edited while keeping both its size *and* its mtime
   is seen as unchanged, so the Backup page has a **Verify file contents again
   (slower)** tick that hashes everything regardless.
2. **An expired session looked like a random failure.** Logging in elsewhere,
   revoking the session, or a deactivated account came back as a generic RPC
   error ("The backup run failed unexpectedly") with no way forward.
   `SessionExpiredError` now covers the whole family (`AUTH_KEY_UNREGISTERED`,
   `SESSION_REVOKED`, `AUTH_KEY_INVALID`, deactivated users, …), the engine
   fails the one transfer that hit it and aborts the run, and the services
   layer reports `auth_state {state: signed_out, expired: true}`. The main
   window then asks whether to sign in again and reopens the sign-in dialog;
   the whole UI refreshes afterwards, so no restart is needed.
3. **A storage whose group was deleted or made private was a dead end.** Batch
   `CHANNEL_PRIVATE`, `PEER_ID_INVALID`, `CHAT_WRITE_FORBIDDEN` and friends now
   map to `StorageUnavailableError`, and the Storages page offers **Repair
   link…**: after an explicit destructive confirmation it creates a fresh
   private forum group with a root topic, clears the stored Telegram ids for
   that storage, and keeps the local index — the next backup uploads everything
   to the new group (re-uploading is the only honest option once the old
   messages are unreachable). Refreshing a vanished storage now says to repair
   instead of failing.
4. **A message deleted from Telegram aborted the whole restore.** If a single
   backed-up message is gone (deleted by hand in the Telegram app), that one
   file now fails with "Run a backup again for this file" and **the rest of the
   run continues**; only a dead session stops the run.

New tests: the full offline first-run journey (sign-in → storage → backup →
unchanged re-run → edited-file replacement → search → single-file and
folder restore), session expiry surfaced from backup and restore, storage
repair (including the "no confirmation, no changes" path), and deleted-message
handling.

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

## 2026-09-16 — scale and long-run behaviour

A backup set of tens of thousands of files is normal, and a Telegram rate limit
is normal too. Exercising both found four more defects:

1. **Telegram rate limits broke a transfer instead of delaying it.** Retrying a
   transfer from its in-flight state was rejected by the state machine
   ("Illegal transition uploading -> uploading"), so the file was marked failed
   even though the error is explicitly retryable (`RateLimitExceeded` carries
   the wait time). Both engines now requeue from every state a retry can start
   in, log each attempt with its reason, and a persistent limit fails the file
   with Telegram's own wording.
2. **The restore page froze on big storages.** The tree created a widget per
   file on the UI thread *and* expanded everything: 50,000 files took 4.2 s of
   frozen UI every time the page was opened. The tree now builds one row per
   folder and materialises a folder's files when it is expanded (0.85 s for the
   same storage, with the rest spent reading the index). Selection is tracked
   per folder, so ticking a folder still selects every file in it - including
   files that were never rendered - and partial selections show as such.
3. **The Retry button did nothing on the rows that needed it.** Only live
   transfers carried their id, so selecting a failed history row and pressing
   Retry (or Pause/Cancel) was silently ignored. History rows are actionable now.
4. **Unbounded growth in long-lived installs.** The transfers table kept every
   finished row forever; startup now trims the history to the newest 200 rows
   (active transfers are never touched). The preview cache is capped by total
   size as well as file count (50 MB / 200 entries), not just by count.

Also: the transfers table no longer rebuilds itself on every progress event
(bursts are coalesced into at most 5 rebuilds/second, identical content is
skipped, and the user's selected row survives a refresh).

New tests: 20,000-file storage trees (lazy rows, per-folder selection covering
unrendered files, select-all and partial states), retry from in-flight states,
persistent rate limits, history pruning, selection survival, progress bursts,
and a bounded-state check of everything the data directory stores.

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

Test count: 256 (1 Windows-only skip).

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
