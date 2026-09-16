# Bug-fix round 1 — the four bugs from the first Windows acceptance test

Uncommitted working-tree changes (nothing pushed, nothing committed) that fix
exactly the four bugs reported after the first real-world Windows acceptance
test of the release build, plus regression tests for each. Everything else in
the application is untouched.

Verification at the end of this round (executed, not assumed):

```text
python -m pytest -q      ->  387 passed, 1 skipped in ~35 s
python -m ruff check teloude/ ->  All checks passed!
```

The single skip is the pre-existing `test_security.py:42 (requires Windows
DPAPI)` mark; both numbers are reproducible on any platform with the documented
test prerequisites (`scripts/ensure_qt_libs.sh` on minimal Linux containers,
`QT_QPA_PLATFORM=offscreen` for the UI tests).

---

## Bug 1 — Restore flattened the selected root folder

**Reported:** restoring into `D:\Restored\` produced `D:\Restored\file1.txt`
and `D:\Restored\Subfolder\file3.pdf` instead of
`D:\Restored\<OriginalRootName>\...`.

**Root cause.** `core/restore.py::RestoreManager._restore_one` builds its
target with `safe_destination(dest_dir, rec.relative_path)`, and the index
stored `relative_path` *relative to the folder the user picked*: a top-level
file was indexed as `file1.txt`, never as `MyStuff/file1.txt`. No layer
recorded the name of the selected root folder, so nothing could be recreated at
restore time - only the nesting below the root survived.

**Fix (smallest correct change).** The selected root folder became the anchor of
every stored path:

* `core/backup.py::backup_root_name()` (new) derives a stable name for the
  selected root (drive/filesystem roots included) and
  `BackupManager.plan()` rewrites every scanned relative path to
  `<root name>/<path inside the root>` before indexing.
* `core/backup.py::BackupManager._adopt_legacy_rows()` (new) re-keys index rows
  written by the previous version: a row whose local path sits inside the
  selected root *and* whose stored path is exactly that file's path relative to
  the root is moved to the anchored path (`FileRepository.move_relative_path()`,
  new). Cloud links, hashes and the backed-up flag are preserved - no
  re-upload, nothing deleted - and a row that already has an anchored twin is
  left alone.
* `BackupPlan.root_name` carries the anchor; restore did not need a new code
  path because `safe_destination()` already keeps the full relative path.

Single-file restore, the four collision choices (Skip/Overwrite/Keep both/
Cancel, including *apply to all*), disk-space checks and size+SHA-256
verification all keep operating on the new target path.

**Files:** `teloude/core/backup.py`, `teloude/infrastructure/repositories.py`.

---

## Bug 2 — The Telegram session did not survive a restart

**Reported:** after phone → code → 2FA, restarting asked for the login flow
again even though the session was still valid.

**Root cause (three parts, all in the auth/startup integration).**

1. `application/services.py::AuthService` never implemented `is_authorized()`,
   although `ITelegramAuth` and the startup gate both assume it exists. The
   gate therefore could not ask the real session for its state.
2. Nothing remembered *which* phone number had been signed in, and the session
   file is named after it (`<session_dir>/<digits>.session`), so no later launch
   could even find the file. `_LazyAuth` (the real-mode auth service) stays
   unresolved - and therefore un-authorized - until a phone number arrives from
   the sign-in dialog.
3. `build_real()` had no startup step that unlocked the stored session, so
   `ui/app.py::run()` fell straight through to `AuthDialog`.

**Fix (reusing the existing secure layer - no second mechanism).**

* `AuthService` gained `is_authorized()`, `remembered_phone()`,
  `forget_phone()` and an optional settings dependency. After a successful code
  or 2FA sign-in it stores **only the phone digits** under
  `telegram.last_phone` (the same digits already implied by the session-file
  name); sign-out forgets them. No session material, password, code or hash is
  ever written, and nothing is logged.
* `ui/app.py::restore_saved_session()` (new) is the startup step: it reads the
  remembered number, unlocks the session file with the **existing**
  `SecureSessionStore` (DPAPI on Windows, unchanged), connects through the
  production connector, and asks `is_authorized()`. It returns `False` - never
  raises, never guesses - for a missing marker, a damaged/locked blob, a
  network failure or a session Telegram no longer accepts, and then the normal
  sign-in dialog runs.
* `ui/app.py::run()` only shows `AuthDialog` after that attempt fails, and
  `MainWindow` now shows the current session state instead of a stale "Ready.".
* The session-file locking/unlocking cycle (`SecureSessionStore`) is unchanged:
  locked on shutdown, unlocked by the same call the first sign-in uses.

**Files:** `teloude/application/services.py`, `teloude/infrastructure/repositories.py`
(`SettingsRepository.delete`), `teloude/ui/app.py`, `teloude/ui/main_window.py`,
`teloude/infrastructure/telegram/session_manager.py` (docstring: it previously
claimed DPAPI was not implemented - it is implemented in
`infrastructure/security/dpapi.py` and used by the composition root).

---

## Bug 3 — Pause/Resume gave no visible state feedback

**Reported:** the Pause/Resume buttons were always enabled, clicking them while
the worker was not at a safe point silently did nothing, and the Restore page
had no Pause/Resume at all.

**Root cause.** `core/control.py::EngineControl` was a pair of `threading.Event`
flags with no notion of a *reported* state, `ui/bridge.py` forwarded no control
event, and both views kept a fixed button layout (`backup_view.py` enabled Pause
and Resume permanently; `restore_view.py` only had Cancel) whose status text was
overwritten by progress lines.

**Fix.**

* `core/control.py` now reports the state the worker confirms:
  `RUNNING → PAUSING → PAUSED → RESUMING → RUNNING`, plus `CANCELLING` and the
  terminal `RunOutcome`s (`completed`, `completed_with_errors`, `failed`,
  `cancelled`). `PAUSED` is entered inside `wait_if_paused()` (the worker is
  genuinely parked) and `RUNNING` is restored only when the transfer
  demonstrably moves again - never on the click. A command that cannot change
  anything is ignored: Pause while pausing/paused/cancelling, Resume while
  running/resuming/cancelling (resuming a *pending* pause is still allowed, so a
  mistaken Pause cannot trap the user).
* `application/services.py` attaches a listener that publishes every transition
  as a `transfer_state` event (`{kind, state}`), emits the run's real starting
  state and exactly one outcome, and exposes `BackupService.state` /
  `RestoreService.state` (`None` when nothing runs) so a view can ask instead of
  guess.
* `ui/bridge.py` forwards `transfer_state`; `ui/views/run_state.py` (new) holds
  the single set of rules/labels shared by both pages; `backup_view.py` and
  `restore_view.py` render them - status line `Uploading:`/`Restoring:`/
  `Pausing…`/`Paused`/`Resuming…`/`Completed`/`Failed`, buttons
  `⏸ Pause` / `▶ Resume` enabled only when they can change something, Restore
  gaining the Pause/Resume buttons (and disabling *Restore entire storage…*
  while a run is live).

**Files:** `teloude/core/control.py`, `teloude/application/services.py`,
`teloude/ui/bridge.py`, `teloude/ui/views/run_state.py` (new),
`teloude/ui/views/backup_view.py`, `teloude/ui/views/restore_view.py`.

---

## Bug 4 — Two root folders shared one Telegram topic

**Reported:** Folder B, backed up explicitly into an existing storage, landed
in Folder A's topic.

**Root cause.** The same missing anchor as Bug 1: every file was indexed
relative to the folder the user picked, so files at the root of *any* selected
folder produced `relative_dir = ''`, which resolved to the storage's own
pre-created "root" folder row (named after the storage) and therefore to a
single topic. `core/topics.topic_name_for()` builds titles from the storage name
and `ensure_topic()` matches exact titles, so both folders were forced into one
thread by construction.

**Fix.** Because each stored path now starts with its root folder's name,
`BackupManager._ensure_location()` resolves a different `folders` row per root
folder - and that row already carries `telegram_topic_id` / `topic_name`. The
topic is created on the first backup of a root folder (via the existing
`topic_chain()` naming, `<storage> / <folder>`), reused on later runs, and the
mapping survives restarts because it *is* the local index (spec §8). No new
supergroup per folder, no second mapping system, no schema change, and the flat
Telegram topic model is untouched.

**Files:** `teloude/core/backup.py` (the anchor from Bug 1 is the whole fix),
`teloude/infrastructure/telegram/fakes.py` (`FakeFileGateway` now records which
topic every document was posted to, so tests can assert delivery).

---

## Tests

New files (+66 tests, all passing):

| File | Tests | Covers |
| --- | --- | --- |
| `teloude/tests/test_root_folder_restore.py` | 12 | Bug 1: root folder + hierarchy restored, deep trees, single-file restore, all four collision choices, traversal refusal, two roots side by side, pre-fix index re-anchored, rows outside the selected root untouched |
| `teloude/tests/test_root_folder_topics.py` | 13 | Bug 4: the ten acceptance scenarios (one supergroup per storage, per-root topics, distinct thread ids, A→A / B→B delivery, reuse on re-run, no fallback to the last used topic, other storage → other supergroup, mapping survives reload) plus topic naming and drive-root naming |
| `teloude/tests/test_pause_resume.py` | 29 | Bug 3: control state machine (PAUSING ≠ PAUSED, worker-confirmed transitions, repeated commands ignored, cancel never resumes, broken observer tolerated), the button/label rules, `transfer_state` sequences for backup *and* restore (real fakes, real services), failure/cancel/idle outcomes, and offscreen view tests including a live run paused and resumed through the real buttons |
| `teloude/tests/test_session_persistence.py` | 12 | Bug 2: session written and locked on exit, silent restore on the next launch, revoked/damaged/unreachable session falls back to sign-in, fresh profile still signs in, 2FA path, sign-out forgets the number, no secrets in the settings table, `AuthService.is_authorized()` contract |

Added to `teloude/tests/test_real_wiring.py` (+2, over the real Telethon request
classes): two root folders into one storage produce two topics and deliver each
file to its own topic (and a re-run reuses the topic), and a storage restore
recreates the selected root folder.

Updated existing tests (behaviour they pinned changed on purpose, because the
stored path gained the root folder): `test_engines.py`, `test_edge_cases.py`,
`test_first_run_journey.py`, `test_recovery_flows.py`, `test_services.py`,
`test_ui.py` - assertions now expect `MyStuff/file1.txt`, `dest/MyStuff/file1.txt`
and the restore tree's `nested`/`nested/sub` rows.

Full suite: **387 passed, 1 skipped** (baseline before this round: 319 passed,
1 skipped; +68 tests). Ruff: **All checks passed!**

## What was verified with mocks and what still needs Windows

Telegram itself is not reachable from this environment, so *no live-Telegram
behaviour was verified* in this round. Everything Telegram-facing is verified
against two doubles: the in-memory gateways (`fakes.py`, which record the exact
topic each document is posted into) and `ScriptedRawClient` in
`test_real_wiring.py`, which speaks the real Telethon request classes through the
production gateways. Real DPAPI encryption is Windows-only, so the session tests
use a protector double to prove the store's lock/unlock/reject-damaged-blob
behaviour; the DPAPI marshalling itself is covered by `test_security.py`
(the Windows-marked test is the one skipped here).

Manual checks that remain for the next Windows run:

1. **Bug 1** — back up a folder with top-level files, restore the storage into a
   fresh `D:\Restored`, confirm `D:\Restored\<folder>\...`. Then restore a single
   file and confirm it lands in its subfolder.
2. **Bug 1 (upgrade path)** — run a backup again for a folder indexed by the
   previous build and confirm nothing is re-uploaded (the rows are re-anchored),
   then restore.
3. **Bug 2** — sign in (incl. 2FA), close Teloude, start it again: it must go
   straight to the dashboard (status bar `Telegram: authorized`) with no code or
   password prompt. Then sign out and restart: the sign-in wizard must appear.
4. **Bug 2 (frozen build)** — repeat 3 with the installed
   `Teloude-Setup-<version>.exe`, and confirm `%APPDATA%\Teloude\sessions`
   contains a `.locked` file (not a plaintext `.session`) while the app is
   closed, and that the file is DPAPI-protected.
5. **Bug 2 (revoked session)** — sign the account out from another device, start
   Teloude: the sign-in prompt must appear (this exercises the path where
   `is_user_authorized()` is false, which the offline tests cover with a double).
6. **Bug 3** — start a backup, hit Pause mid-upload: the status must read
   `Pausing… → Paused`, Resume must be the only enabled control, and Resume must
   continue the same transfer. Repeat for a restore. Confirm double-clicking
   Pause/Resume cannot leave the UI claiming a state that is not happening.
7. **Bug 4** — back up Folder A and Folder B into the *same* storage; the
   supergroup must contain one topic per folder (named `<storage> / <folder>`)
   with each folder's files in its own topic, and a second backup of Folder A
   must not create another topic.
8. Check the log file for the absence of phone numbers, codes, 2FA passwords,
   API hashes or session contents after these steps.

## Notes / limitations

* The session file is plaintext **while the app runs** (Telethon's own format);
  the existing DPAPI-at-rest protection (locked on clean shutdown, unlocked at
  startup) is unchanged. A hard kill therefore leaves the plaintext session on
  disk until the next clean shutdown - pre-existing behaviour, not introduced
  here, and it is what makes "reopen silently" possible.
* Index rows written by earlier builds are migrated lazily: they move to the
  anchored path the next time that folder is backed up. Until then such rows
  keep restoring the old (flat) way - no data is lost or re-uploaded either way.
* The `transfer_state` UI behaviour is covered by offscreen Qt tests; the visual
  result (labels, button states, emoji) still needs one human look on Windows.
