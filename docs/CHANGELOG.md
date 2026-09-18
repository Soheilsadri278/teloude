# Changelog

Notable changes, newest first. Versions are milestone commits, not releases.

## 2026-09-18 — Uploads 6–23× faster; the proxy page becomes glass; completion notifications

Three changes from one round of user reports.

### Uploads were slow because every part waited for the previous one

A backup uploaded one 128 KiB part per round trip: throughput was capped at
`part_size / RTT`, so a normal 60 ms line to Telegram moved about **2.2 MB/s**
(measured) no matter how fast the connection was. Two causes, both fixed:

* **The configured chunk never reached the gateway.** `chunk_size_kb`
  (default **512 KiB**, the MTProto maximum) existed in the configuration but
  nothing read it, and Telethon's own table picks **128 KiB** for files under
  100 MiB. The engines now take the configured size and pass it down (uploads
  as the part size, downloads as the GetFile chunk), clamped to what MTProto
  accepts (16–512 KiB, 4 KiB-aligned).
* **Parts were strictly sequential.** Telegram's `saveBigFilePart` numbers its
  parts and reassembles at commit, so several parts of *one* file may be in
  flight — that is what official clients do, and it still transfers exactly
  one logical file at a time. The gateway now keeps up to 8 parts
  unacknowledged per file (injectable `window=1` restores the old loop).
  Progress stays an absolute, ordered prefix; md5 and bytes are identical to
  the sequential loop (pinned by equivalence tests); pause/cancel keep their
  checkpoints (a pause waits for the flying parts before raising, so nothing
  is re-sent on resume); a part failure drains every in-flight answer before
  surfacing, and the engine's retry path restarts from the checkpoint exactly
  as before. Per-part checkpoint writes measured ~0.2 ms — negligible, and
  unchanged in behaviour.

Measured on a reproducible benchmark (loop thread + gateway + registry
checkpoints, simulated 60 ms RTT): **2.2 MB/s → 50.2 MB/s**, a 23× improvement
that scales with the real round-trip time. Tests: `teloude/tests/test_upload_pipeline.py`.

### The proxy page is a translucent sheet

The connection settings dialog is now the application's one Apple-style glass
surface: frameless, rounded 20 px, with the true glass fill from the token set
(`rgba(255,255,255,.72)` light, `rgba(28,28,30,.70)` dark — dark mode keeps
working), a hairline border, a top highlight and one drop shadow. It is real
OS-composited translucency (`WA_TranslucentBackground`), not a screenshot or a
fake blur — Qt cannot blur the pixels behind a window, so the scrim dims the
app behind the sheet instead and nothing readable floats over raw
transparency. The sheet drags by its passive areas, stays resizable, and every
control keeps its 44 px target and its previous behaviour (test-first
connect, secret masking, the icon). Tests: `TestTheGlassSheet` in
`teloude/tests/test_proxy_ui.py`.

### Backup and restore say goodbye

A run's completion now raises a system notification through the existing tray
icon — "Backup completed / Your backup has finished successfully.", likewise
for restore, and "…failed" variants naming the first files that failed. The
notifications ride the services' own `backup_done` / `restore_done` events
(lifecycle, not timers, no widgets), appear even when the window is closed to
the tray, never fire for a cancelled run, never claim success for a partial
one, and an event that is delivered twice cannot notify twice. Without a
system tray the text degrades to the log, never to a modal. Tests:
`teloude/tests/test_notifications.py`.

## 2026-09-18 — A real proxy secret was refused as "not usable"

Someone pasted a secret from a working proxy, ``EERighJJvXrFGRMCIMjdCQ``, and the
dialog answered *"That secret is not usable"*. The secret was fine: it is the
base64 of the 16 key bytes ``1044…dd09``, in exactly the form Telegram and the
transport accept.

* **The cause was ours.** The ``dd``/``ee`` marker at the start of a secret is
  recognised **only in lower case** - that is how Telegram writes it and how the
  transport reads it, and it is what makes ``EERigh…`` (data that happens to
  begin with those letters) different from ``ee0011…`` (a fake-TLS marker). The
  check lower-cased the first two characters before comparing, so it cut ``EE``
  off a base64 secret, left 15 bytes instead of 16, and rejected a key that
  would have connected.
* **Teloude's check is now the transport's check.** Deriving the key follows the
  same algorithm the connection uses, so "usable" means exactly "the connection
  can derive a real key from this text" - being cleverer than the transport is
  how a working secret got refused. A contract test compares both sides for
  every shape Telegram shows (hex, upper-case hex, both markers, marker with
  domain bytes, base64 padded and unpadded, and the base64 above).
* **Proven on the wire, not just in the settings.** `teloude/tests/test_proxy_transport.py`
  runs a mock MTProto proxy - the server side of the handshake, written from the
  protocol rather than from Telethon's client code - on localhost, and checks
  that the client Teloude builds really produces traffic that a proxy holding
  the secret can read back as valid MTProto (`req_pq_multi`) for every secret
  shape Telegram hands out. The negative control matters as much: with any other
  secret those same bytes are unreadable, so the secret genuinely binds the
  traffic. There is no network beyond localhost, and no account, in that test.
* **One shape is refused on purpose, with a sentence.** ``EE0011…`` (an
  upper-case marker) would be read as a 17-byte secret and connect with the
  wrong key; it is now named as such - "Telegram writes that marker in lower
  case, paste the secret exactly as Telegram shows it" - instead of failing
  later with a proxy error that says nothing about the cause. The message for
  any other unusable text now also says how many characters were pasted, so a
  secret copied one character short is visible.

## 2026-09-18 — A proxy that belongs to the connection, not to the login form

Teloude can now be pointed at an MTProto proxy the way Telegram itself does it:
a small connection icon shows what the Telegram connection is doing, and clicking
it opens the proxy page. The important half is architectural - the proxy is a
property of the *connection*, not of the login screen.

**One connection for the whole application.** `infrastructure/telegram/connection.py`
is the single owner of "how Teloude talks to Telegram": it holds the proxy
configuration, builds the client, tracks the state, and hands the one callable
(`connect(phone)`) that `ui/app.py` wires into the rest of the stack. Because
authentication, session creation, uploads, downloads, syncs and searches all
derive their client from that same client - the gateways are built from its raw
Telethon client - a proxy set here is the proxy everything uses. No feature
grows its own proxy handling, and none of them can drift onto another transport.

**The icon.** `ui/connection_indicator.py` paints four states, animated rather
than switched: a quiet grey ring when disconnected, a rotating accent arc while
connecting, a green disc with a check mark (spring pop) when connected, and a red
disc with an exclamation mark that shakes into place on error. State changes
cross-fade over the design system's 300ms. It sits in the sign-in wizard's corner
(before authentication) and in the main window's status bar (after it); both open
the same page, and both are fed by the same connection layer, so the two can
never disagree.

**The page.** `ui/proxy_dialog.py` holds type, server, port, secret and an
enable switch, plus *Test connection* (probe the endpoint on a throwaway session;
nothing is saved) and *Connect* (save, test, and then move the live session onto
it - the test runs first, so a typo cannot break a working session, and a
successful switch re-points every gateway at the new client without a restart).
The login form itself is unchanged: phone, code, password.

**Security.** The proxy secret is stored through the existing secure layer -
`ProxySettingsStore` writes the base64 of DPAPI-protected bytes (Windows,
current user), with the explicitly labelled plaintext fallback elsewhere - never
in the clear in the settings table, never in a log line, never in a status label
or tooltip. `ProxyConfig.secret` is excluded from `repr()`, the layer redacts any
secret that a third-party error quotes back before it can be shown, and the
secret field is masked with an explicit reveal.

**Nothing changes when no proxy is configured.** A client without a proxy is
built with exactly the same three arguments as before (`proxy` is only ever
passed when one is configured), the offline stack keeps working on its scripted
client, and `build_real(..., connector=...)` still accepts a scripted connector.

Tests: `teloude/tests/test_proxy_connection.py` (configuration, storage, the
four-state model, the probe and its timeout, redaction, and that one proxied
client reaches both the auth flow and the gateways - including after a switch),
`teloude/tests/test_proxy_ui.py` (the icon's states, colours and animations, the
wizard having no proxy fields, the page's behaviour, and that the secret is
nowhere on screen).

## 2026-09-17 — The two Windows failures the annotations named

The first Windows run that could name its failures (see the entry below) named
exactly two: both assertions that hold only on POSIX. Nothing in the watchdog or
in the workflow changed behaviour - the tests now assert what each platform
really guarantees, and the reason is written down where the next reader will
look.

* **`test_a_killed_but_unreaped_process_is_not_alive`** treated `Popen.wait()`
  as "reaped". On Windows it is not: the process object - and the pid that a bare
  `OpenProcess`, the naive check this test uses, still resolves - stays alive
  while *any* handle to it is open, and `Popen` holds one until the object is
  collected. The test now drops that last handle (`victim = None` plus
  `gc.collect()`) before requiring the naive check to agree the pid is gone; on
  POSIX the wait *is* the reap, so nothing changes there.
* **`test_the_supervisor_kills_a_run_that_cannot_report_progress`** required the
  victim's stacks in the report - which is the POSIX path: faulthandler gets
  SIGABRT before the supervisor escalates. Windows has no equivalent at all
  (`taskkill /F` is TerminateProcess: no signal, no cleanup, no dump), so on
  Windows the test asserts what that path can prove - the stranded test is
  named, the kill is confirmed, the run fails - and POSIX keeps the stack
  assertion. Windows still gets stacks wherever they can be had: the in-process
  watchdog writes them itself, and the C-level guard writes them even while the
  GIL is held. The module docstring now says that instead of claiming "every
  thread stack dumped" on every platform.

Both failures were found by the annotations added below, on the first run that
had them: the run page named the test, the file and the line, instead of
"exit code 1".

## 2026-09-17 — A red run explains itself instead of saying "exit code 1"

No Windows run of the release workflow has ever reached its packaging steps, and
each time the Tests step stopped it the run page said one thing: "Process
completed with exit code 1". The failing test was only findable by reading a
3000-line step log, and a run that was *killed* (a watchdog firing, the
concurrency group cancelling the job) left no record at all - even though the
workflow is supposed to be diagnosable from what it leaves behind.

* **The Tests step keeps its output.** `pytest -v --durations=25` now also writes
  `--junitxml="$env:RUNNER_TEMP\pytest-windows.xml"` and tees the whole run to
  `pytest-windows.log`, so the diagnosis no longer depends on the job log.
* **Every failing test becomes an annotation.** The new `Report the failing
  tests` step reads the JUnit report and emits
  `::error file=<path>,line=<n>::<message>` (`%`, CR and LF escaped, capped at
  25) - the failing test, its file and its line on the run page, with no step to
  open. A killed run has no report, so the step then prints the last
  `FAILED`/`ERROR` lines the log does carry, plus the last 120 lines of output.
* **A failing run keeps its evidence.** `Upload the test report` (only on
  failure) publishes the pytest output, the JUnit report and the hang-watchdog
  report as `teloude-test-report-<sha>`. Before this, a failed run uploaded
  nothing whatsoever: the only artifact was the installer of a run that passed.
* `teloude/tests/test_release_packaging.py` pins the new contract - the JUnit
  file, the tee, the annotation, the artifact, and that the failure report runs
  before the packaging steps - next to the guarantees that were already there.

## 2026-09-16 — A blocked test fails CI in seconds (and the live pause test is deterministic)

The first Windows CI run sat in its Tests step for 42 minutes with no output and
nothing naming the test that was stuck, and the live pause/resume test could not
be trusted to describe the same run twice. No test was skipped, deleted or
weakened, and no timeout was raised to hide anything.

* **The live run is deterministic.** `TestLivePauseFromTheUi` now drives Pause,
  Resume and Cancel through the real buttons, on a transfer that is deliberately
  held mid-file, so "the transfer is moving -> the user clicks -> the engine
  parks" cannot race the end of the file any more. Every wait is bounded and
  every failure message carries the evidence (state, parts uploaded, button
  matrix, page text). The old version asked the *worker thread* to request the
  pause from a progress callback, which is not what the acceptance test
  describes and which hid the real finding: a disabled button silently swallows
  the click.
* **Two hang watchdogs, both bounded.** `teloude/tests/watchdog_plugin.py`
  (wired in through `teloude/tests/conftest.py`) fails the run in 30s
  (`TELOUDE_TEST_WATCHDOG`) with the exact pytest node id, the stack of every
  thread and exit code 97, and a separate supervisor process kills the run in 45s
  (`TELOUDE_TEST_GUARD`) when there is no progress at all - the case where Python
  itself cannot run the in-process watchdog. The single 150s timer this replaces
  was both too slow to be useful and blind to a block inside native code.
  Neither watchdog can wait forever, neither signals a pid that is no longer the
  test run, and both stand down the moment pytest exits. They report into
  `TELOUDE_WATCHDOG_FILE`, alongside faulthandler crash dumps. They are a
  diagnostic, not a looser timeout: a test that trips one has not finished, which
  is a failure either way.
* **Both layers are proven on a deliberately blocked test.**
  `teloude/tests/test_hang_watchdog.py` runs real pytest sessions against a
  deadlocked probe through the same conftest wiring CI uses and pins the outcome:
  exit code 97 with the node id and all stacks, the supervisor's kill with the
  victim's stacks even when the in-process watchdog is off, no watchdog process
  surviving the run, the wiring in the conftest being complete, and the
  supervisor's embedded program compiling (a stray newline in it once left a
  watchdog that looked alive and guarded nothing).
* **CI names each test as it starts.** The Tests step runs `pytest -v
  --durations=10` with `PYTHONUNBUFFERED`, publishes the watchdog report in a
  `if: always()` step, and the packaging guards in
  `teloude/tests/test_release_packaging.py` pin all of it.
* **Teardown leaves nothing behind.** `AppContext.shutdown()` only cancels a run
  that is still live (a finished run is not "cancelled by shutdown"), and
  `ServiceBridge.detach()` unhooks the UI from the event bus before the widgets
  disappear, so a late `transfer_state` can no longer land on whichever page
  runs next. `EventBus.unsubscribe()` is the small API that makes it possible.
* Regression tests: live Pause -> Resume -> "Uploaded 2" through the buttons,
  live Cancel, shutdown-without-a-stale-event, and no updates delivered after
  shutdown (all four fail against the previous behaviour).

Multiple drive-by checks (3 isolated runs, 40 runs under CPU load, the whole
suite twice) never reproduced the 42-minute block on Linux, which is why the
watchdog exists: the next occurrence names itself.

## 2026-09-17 — Termination no longer depends on a lock, a live Python thread or the GIL

A Windows run died on `test_scale.py` with exit code 1 and no explanation. The
fixture was too slow, the in-process watchdog fired, and its own stack capture
then blocked: the exit it had promised never ran and the supervisor killed the job
15s later. The previous round moved the arm/verify order around, which was not
enough - the guard it armed was a Python thread, and the code before it touched
the report lock. Two holes are closed here, and the bound is now stated as it
really is.

* **There is a guard that needs neither a Python thread nor the GIL.**
  `faulthandler.dump_traceback_later(..., exit=True)` keeps its timer on a thread
  created inside the C module: it dumps every thread stack into the report and
  calls `_exit(1)` itself. It is armed as the first action of `_bail()`, before the
  verdict, the annotation, the snapshot or the dump - so a diagnostic that blocks,
  or one that holds the GIL, cannot keep the run alive. Proven against a
  `ctypes.PyDLL` call that holds the GIL (the portable way to make a C call that
  never releases it) on Python 3.12 and 3.13.
* **Nothing on the termination path waits for `_report_lock`.** The verdict, the
  annotation and the exit line go out through raw `os.write()` on an append
  descriptor; the snapshot and the faulthandler dump are best effort and are cut
  off by the guard. `_say()` itself now takes the lock with a deadline and falls
  back to the same raw write, so no logged line - session start, test header,
  snapshot frame - can wedge the session either.
* **The heartbeat only vouches for a test that is in flight.** "Some Python thread
  can still run" is not evidence that pytest is healthy: a session stuck in
  collection, in a report write or in a diagnostic used to borrow that credibility
  and hang until the job limit. The heartbeat stops at the first sign of the run
  being over (`_TERMINATING`), says nothing between tests, and the supervisor -
  which needs no cooperation from the process - then acts on its own monotonic
  clock. When the report already carries a `HANG`, its patience drops to
  `TELOUDE_TEST_HANG_GRACE` instead of sitting out the whole guard window.
* **The diagnostic window is the only thing diagnostics get**, and the bound is
  documented honestly: in-process termination within
  `TEST_LIMIT + max(DUMP_GRACE, HANG_GRACE)` (35s in CI), absolute worst case
  `TEST_LIMIT + GUARD_LIMIT` (75s) when nothing inside the process can run. The
  limits themselves are unchanged: 30s, 45s, 3s.
* Regression tests, all deterministic: the guard exits while the GIL is held (bare
  interpreter, the mechanism itself); a GIL-holding diagnostic still terminates
  within the bound and names the test; a thread that holds `_report_lock` forever
  cannot stop the session, the verdict or the exit; a session wedged in collection
  is killed by the supervisor and reported by name; a wedged diagnostic cannot keep
  the supervisor asleep. The termination path is additionally pinned as
  "guard first, no lock" by reading its source.

## 2026-09-17 — The watchdog terminates under all conditions, and the scale fixture stops benchmarking SQLite

A Windows run died on `test_scale.py::TestRestoreTreeAtScale::
test_tree_is_lazy_and_fast_for_twenty_thousand_files` with exit code 1, no
FAILED line and no explanation on the run page. The fixture had grown past the
per-test limit; the in-process watchdog fired, and its own stack capture then
blocked - so its exit never ran and the supervisor killed the job 15s later.
No test was skipped, no assertion weakened, no limit raised.

* **Termination outranks diagnostics.** On expiry the watchdog now writes the
  verdict and the annotation, arms a hard exit, takes a Python-level snapshot of
  every thread (which suspends nothing, so it cannot deadlock) and only then
  attempts the faulthandler dump. Whatever blocks, the run ends within
  `TELOUDE_TEST_WATCHDOG + TELOUDE_TEST_DUMP_GRACE` (30s + 3s) - inside the 45s
  supervisor window, so the safety net no longer depends on the thing it is
  diagnosing. (The arm/verify order alone did not make that bound true: a
  Python-level exit can itself be blocked by a stack capture or a held GIL, so the
  entry above replaces the guard `_bail()` arms with the C-level one and takes the
  report lock off the termination path entirely.)
* **Progress is an explicit heartbeat.** The session rewrites a sequence number
  while it runs; the supervisor compares it against its own monotonic clock and
  never against the file's mtime or the wall clock, so a slow test stays alive and
  a clock step cannot fake progress or cause a kill. A heartbeat also names its
  session pid, so a stale file cannot be read as progress, and the supervisor
  stands down the moment pytest is gone.
* **Honest verdicts.** When the report already shows the in-process watchdog
  fired, the supervisor now says its exit was blocked instead of claiming the
  watchdog was silent.
* **The scale fixture seeds in batches.** `_seed_files` writes the 20,000 rows in
  one transaction and marks them backed up in one statement instead of 60,001
  round trips; the module went from 38s to 22s and a slow runner can no longer
  push a fixture over the per-test limit. Same files, same database state, same
  laziness assertions; `upsert()` and `mark_backed_up()` keep their row-by-row
  coverage in the repository tests.
* **GitHub annotations.** Both watchdogs emit `::error title=Hang watchdog::<node
  id> - <reason>`, straight to the job log (the in-process one through the
  descriptor it captures from behind pytest's output capture, the supervisor
  through the descriptor it inherits), so the stuck test is visible on the run
  page without opening a step. The detailed report is unchanged.
* Regression tests: a blocked diagnostic cannot prevent bounded termination
  (probe replaces faulthandler's dump with something that never returns); a
  progressing test is not killed; a run with no heartbeat is killed and named; a
  `SIGSTOP`ped process is killed; the annotation reaches both report and job log;
  the termination bound stays inside the supervisor window; the fixture seeds in
  fewer than 20 statements and still holds 20,000 backed-up files.

## 2026-09-16 — Apple-inspired design system and the Liquid Glass surfaces

The UI gained a design system and the four translucent chrome surfaces it was
approved for. Content stays opaque; no feature, no logic and no architecture
changed.

* **`teloude/ui/theme.py`** - one source of truth for the visual constants: 8pt
  spacing, radii 8/12/20, 44px minimum targets, SF Pro with a native system
  fallback, `#007AFF`/`#0A84FF` accents, the 300ms standard and spring curves,
  and the generated stylesheet plus palette for light (default) and dark. The
  app previously had no stylesheet at all; the dashboard's one inline
  `font-size` rule moved here. `normalize_layout_spacing()` snaps the platform's
  default 9/11px margins and gaps onto the grid - layout properties only.
* **`teloude/ui/components.py`** - the surfaces themselves: a translucent
  `NavPanel`, a `NavRail` with a text-only `NavItemDelegate` that paints the
  selection capsule, a `FloatingBar` whose shadow deepens while a transfer is
  genuinely live, a shared `Scrim` that dims the window behind modal surfaces,
  the `GlassCard` dialog surface with its spring entry, and `present_blocking()`
  so message boxes dim the app behind them like every other overlay.
* Appearance is switchable on the Settings page (Light/Dark, applied
  immediately) and remembered through the existing settings service
  (`ui.appearance`) - no new storage mechanism.
* Navigation is text-only: no badges, no numbered circles, no decorative icons.
* `teloude/tests/test_ui_theme.py` (34 tests) checks the tokens, the measured
  contrast of text on the composited glass surfaces, the 8pt grid across every
  layout in the window, that the rail stays text-only, that the action bar lifts
  only for live states, and that the scrim nests and never swallows input.

*Qt limits, honoured rather than faked:* Qt cannot blur the pixels behind a
widget, so no `grab()`-and-blur imitation exists here. Translucency is a styled
fill composited by Qt over the window's own canvas (the rail and the toolbar);
a floating bar over a scrolling list cannot be translucent that way, so it uses
the material at dialog strength with a hairline and one restrained shadow
instead. Shipped dialogs stay opaque top-level windows - a translucent window
would need `Qt.FramelessWindowHint`, out of scope for this project.

## 2026-09-16 — Windows release build in GitHub Actions

The installer no longer needs a developer's Windows machine:
`.github/workflows/windows-release.yml` builds it on a Windows runner from any
pushed commit (or by hand with *Run workflow*). It runs Ruff and the test suite,
then PyInstaller and Inno Setup 7.1.0 - downloaded from the official immutable
release and verified against a pinned SHA-256 before it is executed - and
uploads `Teloude-Setup-<version>.exe`, its SHA-256, a build record and the
toolchain freeze as a workflow artifact.

The Telegram credentials come from the repository secrets `TELOUDE_API_ID` and
`TELOUDE_API_HASH`: they are passed to the build step through the environment,
never written into the repository, never logged and never part of the artifact.
A run without them fails in its first step and names what is missing. Secrets
never appear in the workflow file - a test enforces that.

The workflow publishes no GitHub Release: the installer is unsigned, and the
Windows acceptance checks in `docs/installer_build_and_test.md` are still manual.
Seven static tests in `teloude/tests/test_release_packaging.py` guard this
contract (Windows runner, tests and Ruff before packaging, secrets only, artifact
upload, no release, checksum-pinned compiler).

## 2026-09-16 — First Windows acceptance test: the four reported bugs

The release build got its first real-world run on Windows, which produced four
bugs. Fixed in the working tree (see `docs/bugfix_round1_acceptance_test.md`):

* **Restore preserves the selected root folder.** Index paths are anchored at
  the folder the user picked, so restoring a storage into `D:\Restored` now
  yields `D:\Restored\<folder>\...` instead of a flattened top level. Existing
  collision handling, single-file restore and path-traversal protection are
  unchanged, and rows written by the previous version are re-anchored on the
  next backup of the same folder (no re-upload).
* **The Telegram session survives a restart.** After phone → code → 2FA the
  auth service remembers the phone digits (only the digits, in the local
  settings table); the next launch unlocks the existing secure session file,
  connects, validates it and goes straight to the main window. The sign-in
  wizard appears only for a genuinely missing, damaged, revoked or unreachable
  session. `AuthService` also exposes the `is_authorized()` its interface
  promised - it was missing, which is why the startup gate never consulted the
  saved session.
* **Pause/Resume is visible and honest.** The control now reports confirmed
  states (Pausing… → Paused → Resuming… → running) as bus events, and both the
  Backup and the Restore page render them: the status line names the state, the
  buttons read "⏸ Pause"/"▶ Resume" and are enabled only when the command can
  change something, so a repeated Pause/Resume can no longer lie. Restore
  gained the Pause/Resume buttons it never had.
* **One forum topic per backup root folder.** Each root folder backed up into a
  storage gets its own topic (`<storage> / <folder>`), created on first backup,
  reused on later ones, and never shared with another folder - previously
  Folder B reused Folder A's topic because every file was indexed relative to
  the folder the user picked. Still one supergroup per storage; the mapping
  lives in the existing `folders` table (no second mapping system).

## 2026-09-16 — Windows release build: the installer a user can actually run

The v1 tree shipped the Inno Setup script and the PyInstaller spec, but nothing
produced `Teloude-Setup-<version>.exe` from a clean checkout, the installed
application still required Telegram API credentials in its environment, and the
documentation led a reader to `pip install` first. That is fixed:

* **One command builds the release**: `scripts\build_windows.ps1` (and the
  double-clickable `scripts\build_windows.bat`) checks the prerequisites
  (Python 3.9+ 64-bit, PySide6, Telethon, PyInstaller, Inno Setup 6), bakes the
  credentials in, runs PyInstaller, compiles the installer, removes the generated
  credentials file again and prints the installer path, size and SHA-256. An
  optional `-SmokeTest` starts the packaged application offline and fails the
  build if it exits early or logs a traceback.
* **The installed application works without environment variables** (spec
  section 11): a release build ships Teloude's own registered credentials. The
  build script writes them to the gitignored `installer\build_credentials.json`,
  `teloude.spec` puts that file inside the bundle, and `teloude/config.py` reads
  the environment first and the bundle second - so development, tests and support
  overrides behave exactly as before, and a source checkout still never picks a
  released build's identity out of nowhere. Values are never logged, never
  written to the database and never committed (a test asserts the last part).
* **A packaged build without credentials explains itself**: with no console to
  print to, the reason is shown in a window (and always logged). The window only
  appears for a frozen Windows build, so scripted and headless runs keep their
  documented exit code instead of waiting for a click.
* **Installer hardening**: a proper GUID `AppId` (upgrade and uninstall
  identity), `MinVersion=10.0` (spec section 3), version information on
  `Setup.exe`, `CloseApplications`/`UsePreviousAppDir` for upgrades, an
  "Uninstall Teloude" Start Menu entry, the desktop shortcut task unchecked by
  default, and `[UninstallDelete]` limited to the program directory - uninstall
  still keeps `%APPDATA%\Teloude` (database, logs, previews, Telegram session)
  because the local index describes backups living in the user's account.
* **Documentation**: `docs/installer_build_and_test.md` (prerequisites, options,
  what the installer does, the Windows acceptance checklist, troubleshooting,
  optional Authenticode signing), README restructured with an "Install (Windows,
  no Python needed)" section first and the source quickstart kept for developers,
  `pyinstaller` added to the dev extra in both metadata views.
* **Tests**: `teloude/tests/test_release_packaging.py` - 25 tests covering the
  credential resolver (bundle file, broken file, environment precedence, nothing
  logged, source checkouts never reading build credentials), the startup-dialog
  gate, and static guards for the build assets (windowed bundle, valid per-user
  installer, both shortcuts plus optional autostart, uninstall may only delete
  `{app}`, version consistency between `pyproject.toml` and the installer, ASCII
  PowerShell, credentials gitignored).

## 2026-09-16 — Windows / Python 3.14 hardening

The suite was finally run on the delivery target (Windows, Python 3.14) and four
failures appeared that no Linux or 3.13 run could show. Root causes and fixes:

1. **A file replaced by a folder was reported as "permission denied".** Windows
   raises EACCES - not EISDIR - when a directory is opened, so a path that had
   become a folder between scanning and uploading surfaced as
   `[Errno 13] Permission denied: '...\\src\\thing'` (POSIX says "Is a
   directory", which is why the regression test passed on Linux). The condition is
   now named from the path rather than the errno: `core/errors.py` gained
   `path_is_directory()` and a `path=` argument for `local_failure_message()`, the
   backup engine checks the path before re-hashing or uploading it ("there is a
   folder where this file was"), and the run loop translates stray OS errors
   through the same mapping instead of echoing `str(exc)`.
2. **Configuration was validated after the GUI import.** `run()` imported PySide6
   and created the QApplication before checking `TELOUDE_API_ID` /
   `TELOUDE_API_HASH`, so on a machine whose Python has no PySide6 wheel (Python
   3.14 before PySide6 6.10.1, or a partial install) the process died with
   `ModuleNotFoundError: No module named 'PySide6'` instead of saying what was
   missing. `startup_problem()` now runs first and in a fixed order - configuration,
   then the GUI dependency - printing one actionable sentence and exiting with
   code 2 (not configured) or 3 (GUI dependency missing), and logging it too,
   because a packaged windowed build has no console. Credential validation is
   unchanged: a real run without credentials still refuses to start.
3. **DPAPI buffers were freed through `crypt32.LocalFree`.** `LocalFree` is a
   **kernel32** export; crypt32 only re-exported it on older Windows builds, so
   the lookup fails there with `AttributeError: function 'LocalFree' not found`
   (seen with Python 3.14 on Windows). The freeing function is now loaded from
   kernel32 with pinned prototypes (`argtypes=[c_void_p]`, `restype=c_void_p`),
   `DATA_BLOB.pbData` is a raw pointer instead of a `c_char_p` (no 64-bit
   truncation, no NUL-terminated-string semantics), both DPAPI output buffers are
   released through it, and a failing free is logged rather than raised so a leak
   can never lose session data. Empty input now fails closed with a clear message
   instead of a ctypes `ValueError`.
4. **The Windows data-directory test measured the real user profile.**
   `Path.home()` reads `USERPROFILE` on Windows (`HOMEDRIVE`+`HOMEPATH` as
   fallback), never `HOME`, so patching only `HOME` was a no-op and the test
   compared `tmp_path/".teloude"` with `C:\\Users\\<user>\\.teloude`. Production
   behaviour was already correct per spec §9 (Windows: `%APPDATA%\\Teloude`;
   without APPDATA: the user profile) and is unchanged - the test now patches every
   home variable both platforms read, so the fallback path is exercised
   hermetically everywhere.

New tests: the directory-vs-permission mapping for EACCES/EISDIR/real files, a
subprocess run of the real entry point with PySide6 hidden (configuration first,
exit 2, no traceback), the same run with credentials present (actionable "install
PySide6" message, exit 3), and seven DPAPI buffer-ownership tests that pin the
kernel32 lookup, the raw-pointer DATA_BLOB, one free per protected/unprotected
buffer, binary payloads with embedded NUL bytes, the fail-closed empty input, and
the logged-not-raised free failure.

## 2026-09-16 — repository delivery: installable packaging metadata

`pip install -e ".[dev]"` — the first command in the README quickstart — could not
work: `pyproject.toml` carried only a Poetry table, with no PEP 621 `[project]`
and no `[build-system]`, so pip failed with `metadata-generation-failed` and a
fresh clone could not be installed or imported as a package. The file now also
carries standard metadata (`[project]`, `[project.optional-dependencies].dev`,
and the setuptools build backend) with exactly the dependencies the Poetry table
declares, so both pip and Poetry work; the placeholder author string
(`Your Name <you@example.com>`) is replaced by the repository's own GitHub
identity. Verified by installing the package in a scratch copy of the tree.

## 2026-09-16 — real-mode startup fix (release blocker)

`python -m teloude.main` — without `--offline` — died with
`ImportError: cannot import name 'TELEGRAM_API_ID' from 'teloude.config'` before
it could validate anything. The entry point imported two credential constants
that the configuration module never defined, so the application could not start
against a real Telegram account at all; every smoke test of the v1 build ran with
`--offline`, which is why the gap survived until the delivery audit.

`teloude/config.py` now exposes `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`, read at
startup from the `TELOUDE_API_ID` / `TELOUDE_API_HASH` environment variables:
nothing is hard-coded, the values are never logged (a non-numeric id logs only
the variable name) and never written to the database or the repository, and an
unset or invalid id is the explicit "not configured" state. Startup now reaches
the intended validation and prints the actual remedy — set `TELOUDE_API_ID` and
`TELOUDE_API_HASH` — exiting with code 2, so a misconfigured install gets a clear
message instead of a crash.

New tests: the exact import the entry point performs, the environment mapping
(unset, blank, valid, non-numeric), a no-leak check proving an invalid api id
never reaches the logs, and a subprocess run of the real entry point asserting
exit code 2 with the actionable message and neither an `ImportError` nor a
traceback.

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
