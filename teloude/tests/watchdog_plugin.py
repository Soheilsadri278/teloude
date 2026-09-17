# teloude/tests/watchdog_plugin.py
"""Watchdogs: a test that blocks must fail the job in seconds, never hold it.

A blocked test once held the whole CI job: the runner sat there with no output
until somebody cancelled it, and nothing said which test was waiting. Two
independent watchdogs cover that now, and neither of them can wait forever.

1. In-process watchdog, one per test (`_TestWatchdog`). A daemon thread counts
   the wall-clock time of the test that is running. After
   `TELOUDE_TEST_WATCHDOG` seconds it writes the pytest node id, takes a stack
   snapshot of EVERY thread and terminates the interpreter with exit code 97, so
   the job fails at once instead of waiting. Its loop only ever waits with a
   deadline, so the watchdog itself cannot hang.

   Diagnostics never outrank termination. On Windows a stack capture can block
   (faulthandler reads another thread's stack by suspending it, and a thread that
   is suspended while holding a runtime lock can deadlock the capturing thread -
   Raymond Chen, "The case of the UI thread that hung in a kernel call", 2025).
   So the watchdog arms a C-level guard before it writes anything, takes any lock
   or starts any diagnostic, and only then reports and dumps:
   `faulthandler.dump_traceback_later(..., exit=True)` runs its timer on a thread
   created inside the C module - it needs no Python thread to be scheduled, no
   lock and no GIL - dumps every thread stack into the report and calls `_exit()`
   by itself. The verdict and the annotation are written with raw `os.write()` on
   an append descriptor, so they do not wait for `_report_lock` either.

   The bound, stated honestly: the in-process watchdog ends a hang within
   `TELOUDE_TEST_WATCHDOG + max(TELOUDE_TEST_DUMP_GRACE, TELOUDE_TEST_HANG_GRACE)`
   seconds (35s with the CI numbers). If nothing inside the test process can act
   at all - a report write wedged *and* a diagnostic holding the GIL - the
   supervisor still ends it at `TELOUDE_TEST_WATCHDOG + TELOUDE_TEST_GUARD`
   seconds (75s), because the heartbeat stops the moment the watchdog fires
   instead of pretending a dying run is healthy.

2. Supervisor process, one per session (`_SUPERVISOR_SOURCE`). A separate
   interpreter watches a heartbeat file that this process rewrites while a test is
   in flight (never merely because some thread of it is still alive), and never
   again once the in-process watchdog has fired. Every poll it reads the heartbeat, and compares the sequence number it
   sees against its OWN monotonic clock - never the file's mtime and never the
   wall clock, so a clock step cannot fake progress. While the sequence keeps
   moving the run is left alone, however slow individual tests are; when it does
   not move for `TELOUDE_TEST_GUARD` seconds - a wedged interpreter, a block in
   native code, or a suspended process - the supervisor records the test that was
   running, kills pytest (`taskkill /F` on Windows, SIGABRT then SIGKILL
   elsewhere) and exits with a non-zero status. The heartbeat names the session
   pid, so a stale file from an earlier run can never be mistaken for progress,
   and the supervisor stands down the moment pytest is gone instead of signalling
   a pid that may have been recycled.

Both write into one file (`TELOUDE_WATCHDOG_FILE`), which the CI step prints with
`if: always()`, so a hang is explained even though pytest never reached its own
summary. Both also emit a GitHub Actions annotation (`::error title=...::`) naming
the test and the reason, straight to the job log, so the stuck test is visible on
the run page without opening the report step. The in-process watchdog writes it on
the stdout it captured before pytest started capturing, and the supervisor writes
it to the same stream, which it inherits as a file descriptor.

Configuration, in seconds (0 turns that watchdog off):

    TELOUDE_TEST_WATCHDOG   per-test limit        default: 30
    TELOUDE_TEST_NOTICE     "still running" line  default: 15
    TELOUDE_TEST_GUARD      heartbeat limit       default: per-test limit + 15
    TELOUDE_TEST_HEARTBEAT  heartbeat interval    default: 1 (0: only test boundaries)
    TELOUDE_TEST_DUMP_GRACE diagnostic window     default: 3
    TELOUDE_TEST_HANG_GRACE supervisor patience once a HANG is on record   default: max(dump window + 2, 5)
    TELOUDE_WATCHDOG_FILE   report path           default: <temp dir>/teloude-pytest-watchdog.log

The numbers are not arbitrary: the slowest test in this suite takes ~6s on a
developer machine (`pytest --durations`), so 30s detects a hang with room for a
CI runner several times slower instead of hiding slowness. The diagnostic window
plus the per-test limit stays below the supervisor limit, which is what makes
"termination is guaranteed" true rather than aspirational. A test that trips the
watchdog has not finished, which is a failure either way - no assertion is
weakened and no test is skipped.

`teloude/tests/test_hang_watchdog.py` proves both layers on a deliberately
deadlocked test.
"""
import faulthandler
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

#: Exit status used when the in-process watchdog terminates the run. Any non-zero
#: value fails the CI step; a value pytest never uses makes the cause obvious.
WATCHDOG_EXIT_CODE = 97

#: How long the session waits for the supervisor to announce itself. Bounded, and
#: only ever spent when the supervisor is broken.
SUPERVISOR_HANDSHAKE = 5.0


def _seconds_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


TEST_LIMIT = _seconds_from_env("TELOUDE_TEST_WATCHDOG", 30.0)
TEST_NOTICE = _seconds_from_env(
    "TELOUDE_TEST_NOTICE", min(15.0, TEST_LIMIT / 2) if TEST_LIMIT else 0.0
)
GUARD_LIMIT = _seconds_from_env(
    "TELOUDE_TEST_GUARD", (TEST_LIMIT + 15.0) if TEST_LIMIT else 0.0
)
HEARTBEAT_INTERVAL = _seconds_from_env("TELOUDE_TEST_HEARTBEAT", 1.0)
#: How long the diagnostic may take before the process exits without it. Kept
#: comfortably inside the gap between the per-test limit and the supervisor limit.
DUMP_GRACE = _seconds_from_env("TELOUDE_TEST_DUMP_GRACE", 3.0)
#: How long the supervisor waits for progress once the report shows that the
#: in-process watchdog fired but could not finish its own exit. Without this the
#: supervisor would sit out the whole guard window, because a wedged diagnostic
#: can keep the heartbeat thread alive while the run is already over.
HANG_GRACE = _seconds_from_env("TELOUDE_TEST_HANG_GRACE", max(DUMP_GRACE + 2.0, 5.0))
#: How long `_say()` waits for the report lock before writing without it. Short on
#: purpose: the lock only keeps lines from interleaving, it must never be a way to
#: wedge the session.
SAY_LOCK_TIMEOUT = _seconds_from_env("TELOUDE_TEST_SAY_TIMEOUT", 1.0)
REPORT_PATH = os.environ.get("TELOUDE_WATCHDOG_FILE") or os.path.join(
    tempfile.gettempdir(), "teloude-pytest-watchdog.log"
)
HEARTBEAT_PATH = REPORT_PATH + ".heartbeat"
SUPERVISOR_LOG_PATH = REPORT_PATH + ".supervisor.err"


def _dup_fd(number: int):
    try:
        return os.dup(number)
    except OSError:
        return None


# The job log's own descriptors, taken from behind pytest's capture in
# `_capture_real_streams`. From then on the watchdog can always reach the job log,
# no matter what pytest does with stdout and stderr - and so can the supervisor,
# which inherits the descriptor instead of a pipe into a capture file nobody
# prints when a fatal signal ends the process.
_RAW_OUT = None
_RAW_ERR = None

#: Set with a plain assignment - never a lock - the moment the watchdog decides
#: the run is over. The heartbeat thread reads it and stops proving progress, so
#: a wedged diagnostic cannot keep the supervisor believing the run is healthy.
_TERMINATING = False
#: A raw append descriptor for the report. `os.write` on it needs no lock and no
#: Python file object, which is what lets the verdict outrank a stuck `_say()`.
_REPORT_FD = None
_native_guard_armed = False

_report = None
_report_lock = threading.Lock()
_watchdog = None
_supervisor = None
_supervisor_log = None
_heartbeat = None
_current_test = {"nodeid": "between tests", "started": None}
_current_lock = threading.Lock()
_heartbeat_seq = 0


def _capture_real_streams(config) -> None:
    """Duplicates the real stdout/stderr, from behind pytest's capture.

    pytest starts capturing file descriptors *before* it loads conftests (issue
    #93), so by the time this plugin is imported, `os.dup(1)` would duplicate the
    capture file - and everything written to it disappears when a fatal signal
    ends the process, which is exactly how a hang ends. Suspending the capture for
    the two `dup` calls is what pytest's own terminal writer does before printing,
    and it is the only way to still reach the job log from here.
    """
    global _RAW_OUT, _RAW_ERR
    capman = None
    try:
        capman = config.pluginmanager.getplugin("capturemanager")
    except BaseException:
        capman = None
    suspended = False
    if capman is not None:
        try:
            capman.suspend_global_capture()   # fd 1 and 2 are the real ones again
            suspended = True
        except BaseException:
            suspended = False
    try:
        _RAW_OUT = _dup_fd(1)
        _RAW_ERR = _dup_fd(2)
    finally:
        if suspended:
            try:
                capman.resume_global_capture()
            except BaseException:
                pass


def _report_file():
    """The one report every watchdog writes to: a real file, never captured.

    Opened for appending, so the supervisor (a different process, same file) and
    this process can never overwrite each other's lines.
    """
    global _report
    if _report is None:
        _report = open(REPORT_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    return _report


def _raw_write(number, text: str) -> None:
    """Writes straight to the job log, even while pytest captures output."""
    if number is None:
        return
    try:
        os.write(number, text.encode("utf-8", "replace"))
    except OSError:
        pass


def _say(line: str, loud: bool = True) -> None:
    """Records a line in the report and, when it matters, in the job log.

    The report is the reliable sink: it is a real file, so neither pytest's
    output capture nor a sudden exit can lose it. The job-log copy is what makes a
    hang visible while it happens - it goes to the descriptor pytest does not own.

    The lock is taken with a deadline and never waited for indefinitely: a writer
    parked on a wedged file - or any thread holding `_report_lock` - must not be
    able to stop the session, the test heartbeat or a termination. If the lock does
    not come back in time the line is written anyway, with the same raw `os.write`
    the termination path uses. Once the run is ending (`_TERMINATING`) that raw
    write is used straight away: a diagnostic printed after the verdict must not
    spend the timeout waiting for a lock that may never come back.
    """
    message = line.rstrip()
    if _TERMINATING:
        # The verdict is already out and the guard is armed: from here on the raw
        # descriptor is the sink, so a snapshot line cannot burn the lock timeout.
        _emergency_write(message)
        return
    reported = False
    if _report_lock.acquire(timeout=SAY_LOCK_TIMEOUT):     # bounded: never forever
        try:
            stream = _report_file()
            stream.write(message + "\n")
            stream.flush()
            reported = True
        except BaseException:
            pass
        finally:
            _report_lock.release()
    if reported:
        if loud:
            _raw_write(_RAW_ERR, message + "\n")
        return
    _emergency_write(message)               # writes to the report and the job log


def _emergency_write(text: str) -> None:
    """Writes to the report and the job log without taking any lock.

    `_say()` is the normal path, but it waits for `_report_lock`, and a
    termination message must never wait for a lock another thread may hold
    forever. `os.write` on an append descriptor needs neither that lock nor a
    Python file object, and the descriptor was captured before pytest started
    capturing output, so the line reaches the job log as well.
    """
    payload = text if text.endswith("\n") else text + "\n"
    for descriptor in (_REPORT_FD, _RAW_ERR):
        if descriptor is None:
            continue
        try:
            os.write(descriptor, payload.encode("utf-8", "replace"))
        except OSError:
            pass


def _annotate_error(reason: str, nodeid: str = None) -> str:
    """Emits a GitHub Actions annotation naming the test and the reason.

    Workflow commands are read from the step's stdout, so this goes to the raw
    descriptor captured before pytest redirected anything, and into the report as
    well - the run page then shows the stuck test without opening the step.
    """
    line = f"::error title=Hang watchdog::{nodeid or 'unknown test'} - {reason}"
    _raw_write(_RAW_OUT, line + "\n")
    _emergency_write(line)
    return line


def _read_report() -> str:
    """Reads the report back; used for the supervisor handshake."""
    try:
        with open(REPORT_PATH, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _supervisor_tail(limit: int = 400) -> str:
    """Whatever the supervisor managed to say on stderr, for the report."""
    try:
        with open(SUPERVISOR_LOG_PATH, encoding="utf-8", errors="replace") as handle:
            text = handle.read().strip()
    except OSError:
        return ""
    return text[-limit:]


class _TestWatchdog(threading.Thread):
    """Terminates the interpreter if one test runs longer than the limit."""

    def __init__(self, nodeid: str, limit: float, notice: float):
        super().__init__(name="teloude-test-watchdog", daemon=True)
        self._nodeid = nodeid
        self._limit = limit
        self._notice = notice
        self._finished = threading.Event()
        self._armed_at = time.monotonic()

    def finished(self) -> None:
        self._finished.set()

    def run(self) -> None:
        try:
            noticed = False
            while not self._finished.wait(0.25):   # bounded wait: never forever
                elapsed = time.monotonic() - self._armed_at
                if self._notice and not noticed and elapsed >= self._notice:
                    noticed = True
                    _say(f"[watchdog] still running after {elapsed:.0f}s: {self._nodeid}")
                if elapsed >= self._limit:
                    self._bail()
                    return
        except BaseException as exc:
            # A watchdog that dies quietly is worse than no watchdog: a silent
            # hang is exactly what must not happen, so fail the run loudly.
            _say(f"[watchdog] internal error ({exc!r}); failing the run")
            os._exit(WATCHDOG_EXIT_CODE)

    # -- termination path -------------------------------------------------- #

    def _bail(self) -> None:
        """Fails the run; the guard that ends it is armed before anything can block.

        The order of these five steps is the whole guarantee:

        1. `_TERMINATING = True` - one plain assignment, no lock and no I/O. The
           heartbeat stops, so the supervisor stops believing this run is healthy.
        2. `_arm_native_guard()` - a C-level timer that dumps every thread stack
           and calls `_exit()` on its own. It needs no Python thread to be
           scheduled, no lock and no GIL, so it works even while this process is
           wedged in a report write or in a diagnostic that holds the GIL.
        3. The verdict and the annotation, written with raw `os.write()` on an
           append descriptor: no lock, no report handle, nothing to wait for. (If
           the write itself blocks, the guard from step 2 still ends the run.)
        4. A Python-level thread snapshot, then the best-effort faulthandler dump.
        5. `os._exit(97)` when the diagnostics returned; otherwise the guard fires.

        Nothing here can wait on `_report_lock`, and nothing here can be kept
        alive by a thread that holds it.
        """
        global _TERMINATING
        _TERMINATING = True
        self._arm_native_guard()
        _emergency_write("")
        _emergency_write("=" * 78)
        _emergency_write(f"[watchdog] HANG: {self._nodeid} ran longer than "
                         f"{self._limit:.0f}s")
        _emergency_write("=" * 78)
        if _native_guard_armed:
            _emergency_write(f"[watchdog] native guard armed: this process exits in "
                             f"{DUMP_GRACE:.0f}s even if a diagnostic blocks or the "
                             f"GIL is held")
        else:
            _emergency_write("[watchdog] native guard is not armed; the supervisor "
                             "is the guarantee that ends this run")
        _annotate_error(f"ran longer than {self._limit:.0f}s; terminating pytest", self._nodeid)
        self._finished.set()                  # safe now: the guard is already armed
        self._dump_stacks()
        self._terminate("diagnostics finished")

    def _arm_native_guard(self) -> None:
        """Starts the C-level exit, `DUMP_GRACE` seconds from now.

        `faulthandler.dump_traceback_later(..., exit=True)` keeps its timer on a
        thread created inside the C module: it dumps the stacks of every thread
        into the report descriptor and then calls `_exit(1)` itself. Arming it is
        a plain C call - no lock, no file object, no I/O beyond the descriptor
        that was already open - so it can be done before anything that might
        block, which is exactly what makes the bound hold.
        """
        global _native_guard_armed
        if _native_guard_armed or DUMP_GRACE <= 0 or _REPORT_FD is None:
            return
        try:
            faulthandler.dump_traceback_later(DUMP_GRACE, repeat=False,
                                              file=_REPORT_FD, exit=True)
            _native_guard_armed = True
        except BaseException:
            _native_guard_armed = False

    def _dump_stacks(self) -> None:
        """Best effort, in order of safety: snapshots first, faulthandler last."""
        self._dump_snapshot()
        try:
            with open(REPORT_PATH, "a", encoding="utf-8", errors="replace") as handle:
                faulthandler.dump_traceback(all_threads=True, file=handle)
            _say("[watchdog] faulthandler thread stacks dumped")
        except BaseException as exc:
            _say(f"[watchdog] could not dump thread stacks with faulthandler: {exc!r}")

    def _dump_snapshot(self) -> None:
        """Every thread's Python stack, read without suspending any thread.

        `sys._current_frames` needs no cooperation from the other threads, so
        this cannot deadlock - which is why it runs before the faulthandler dump
        and why the CI log still names the blocked frame on Windows even when that
        dump is what blocks.
        """
        try:
            frames = sys._current_frames()
        except BaseException as exc:
            _say(f"[watchdog] could not snapshot thread stacks: {exc!r}")
            return
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        _say("[watchdog] thread snapshot (no thread was suspended):")
        for ident, frame in frames.items():
            _say(f"--- thread {names.get(ident, 'unknown')} ({ident:#x}) ---", loud=False)
            try:
                for line in traceback.format_stack(frame):
                    _say(line.rstrip("\n"), loud=False)
            except BaseException as exc:
                _say(f"[watchdog] could not format that thread: {exc!r}", loud=False)

    def _terminate(self, why: str) -> None:
        """Ends the process with the watchdog status, without touching a lock."""
        _emergency_write(f"[watchdog] terminating pytest with exit code "
                         f"{WATCHDOG_EXIT_CODE} ({why})")
        try:
            _report_file().flush()
        except BaseException:
            pass
        os._exit(WATCHDOG_EXIT_CODE)


def _heartbeat_payload(nodeid: str, running_for) -> str:
    """The heartbeat the supervisor reads: session, sequence, test, runtime."""
    global _heartbeat_seq
    with _current_lock:
        _heartbeat_seq += 1
        sequence = _heartbeat_seq
    age = f"{running_for:.3f}" if running_for is not None else "-1"
    return f"session {os.getpid()}\n{sequence}\n{nodeid}\n{age}\n"


def _write_heartbeat(nodeid: str, running_for=None) -> None:
    """Tells the supervisor that this process is alive and which test it runs.

    Written through a temporary file and `os.replace`, so a reader never sees a
    half-written heartbeat. Every failure is swallowed: the supervisor only ever
    kills a process that could not write for the whole guard window.
    """
    if GUARD_LIMIT <= 0:
        return
    payload = _heartbeat_payload(nodeid, running_for)
    temporary = f"{HEARTBEAT_PATH}.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(payload)
        os.replace(temporary, HEARTBEAT_PATH)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass


class _Heartbeat(threading.Thread):
    """Proves *test* progress: the sequence moves while a test is in flight.

    This is what separates "slow" from "wedged". A test that legitimately takes
    minutes keeps the sequence moving and is left alone. It deliberately says
    nothing at all when no test is running: "some Python thread of this process
    can still run" is not evidence that pytest is healthy, and a session wedged in
    collection, in a report write or in a diagnostic must not be able to borrow
    the heartbeat's credibility. Outside a test window the supervisor's own clock
    runs, so such a wedge is killed at the guard limit."""

    def __init__(self, interval: float):
        super().__init__(name="teloude-heartbeat", daemon=True)
        self._interval = interval
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        try:
            while not self._stopped.wait(self._interval):   # bounded wait
                if _TERMINATING:
                    # The run is over: stop claiming progress, so the supervisor
                    # cannot be kept asleep by a heartbeat from a wedged process.
                    return
                with _current_lock:
                    nodeid = _current_test["nodeid"]
                    started = _current_test["started"]
                if started is None:
                    # No test in flight: nothing to vouch for. Collection, session
                    # setup and the gaps between tests are the supervisor's clock.
                    continue
                _write_heartbeat(nodeid, time.monotonic() - started)
        except BaseException:
            pass          # a heartbeat thread must never take the run down


def _mark_test_started(nodeid: str) -> None:
    with _current_lock:
        _current_test["nodeid"] = nodeid
        _current_test["started"] = time.monotonic()


def _mark_test_finished(nodeid: str) -> None:
    with _current_lock:
        _current_test["nodeid"] = "between tests"
        _current_test["started"] = None


# The supervisor runs in its own interpreter: no lock, no GIL and no Qt object in
# the pytest process can block it. Every loop is bounded, a failure inside it
# still ends with a kill attempt, and it stands down when the session ends, when
# it loses its parent or after the absolute cap (90 minutes).
#
# Progress is read as a sequence number from the heartbeat file and timed with
# this process's own monotonic clock. The file's mtime and the wall clock are
# deliberately not used: a clock step on a fresh runner, or an mtime the
# filesystem does not update, would otherwise look like a hang.
_SUPERVISOR_SOURCE = "\n".join([
    'import os, signal, subprocess, sys, threading, time',
    '',
    'heartbeat, stall, report_path, parent_pid, session_cap, hang_grace = (',
    '    sys.argv[1], float(sys.argv[2]), sys.argv[3], int(sys.argv[4]),',
    '    float(sys.argv[5]), float(sys.argv[6]),',
    ')',
    'gone = threading.Event()',
    "FIRED = '[watchdog] the in-process watchdog fired but its exit is blocked'",
    "BLIND = '[watchdog] pytest cannot report progress at all'",
    '',
    '',
    'def _watch_parent():',
    '    # pytest holds our stdin open; EOF means it exited (or was killed).',
    '    try:',
    "        while sys.stdin.read(1) != '':",
    '            pass',
    '    except Exception:',
    '        pass',
    '    gone.set()',
    '',
    '',
    'threading.Thread(target=_watch_parent, daemon=True).start()',
    '',
    '',
    'def alive(pid):',
    '    # A pid can be recycled: never signal one that is not the pytest we were',
    '    # started for. A zombie counts as gone - it cannot be running tests.',
    "    if os.name == 'nt':",
    '        try:',
    '            import ctypes',
    '            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)',
    '            if not handle:',
    '                return False',
    '            ctypes.windll.kernel32.CloseHandle(handle)',
    '            return True',
    '        except Exception:',
    '            return True',
    '    try:',
    "        with open('/proc/%d/stat' % pid) as handle:",
    "            if handle.read().split(') ')[-1][:1] == 'Z':",
    '                return False',
    '    except Exception:',
    '        pass',
    '    try:',
    '        os.kill(pid, 0)',
    '    except OSError:',
    '        return False',
    '    return True',
    '',
    '',
    'def wait_gone(pid, timeout):',
    '    deadline = time.monotonic() + timeout',
    '    while time.monotonic() < deadline:',
    '        if not alive(pid):',
    '            return True',
    '        time.sleep(0.2)                      # bounded wait',
    '    return not alive(pid)',
    '',
    '',
    'def report(line):',
    '    for _ in range(3):                      # bounded retries: never spin forever',
    '        try:',
    "            with open(report_path, 'a', encoding='utf-8', errors='replace') as handle:",
    "                handle.write(line.rstrip() + '\\n')",
    '            return',
    '        except Exception:',
    '            time.sleep(0.1)',
    '',
    '',
    'def annotate(reason, what):',
    '    # GitHub reads workflow commands from the step stdout, which this process',
    '    # inherited: the stuck test shows up on the run page, not only in the report.',
    "    line = '::error title=Hang watchdog::%s - %s' % (what or 'unknown test', reason)",
    '    try:',
    '        sys.stdout.write(line + chr(10))',
    '        sys.stdout.flush()',
    '    except Exception:',
    '        pass',
    '    report(line)',
    '',
    '',
    'def read_heartbeat():',
    '    # (session pid, sequence, test) - or None when the file is missing or torn.',
    '    try:',
    "        with open(heartbeat, encoding='utf-8', errors='replace') as handle:",
    '            lines = handle.read().splitlines()',
    '    except OSError:',
    '        return None',
    "    if len(lines) < 3 or not lines[0].startswith('session '):",
    '        return None',
    '    try:',
    '        owner = int(lines[0].split()[1])',
    '        sequence = int(lines[1])',
    '    except (IndexError, ValueError):',
    '        return None',
    "    return owner, sequence, (lines[2] or 'unknown')",
    '',
    '',
    'hang_offset = 0',
    'try:',
    '    hang_offset = os.path.getsize(report_path)',
    'except OSError:',
    '    hang_offset = 0',
    '',
    '',
    'def hang_already_reported():',
    '    # True when the in-process watchdog spoke but the process is still here:',
    '    # that means its own exit is blocked (a stuck stack capture, a held GIL).',
    '    # Only lines written after this session started count, so a stale HANG line',
    '    # from an earlier run can never shorten this one.',
    '    try:',
    "        with open(report_path, 'rb') as handle:",
    '            handle.seek(hang_offset)',
    "            return b'[watchdog] HANG:' in handle.read()",
    '    except OSError:',
    '        return False',
    '',
    '',
    'def kill_pytest():',
    '    # Returns True only when the pytest process is really gone, so the report',
    '    # can say what happened instead of claiming a kill that never landed.',
    '    if not alive(parent_pid):',
    "        report('[watchdog] pytest process is gone; nothing to kill')",
    '        return False',
    "    if os.name == 'nt':",
    '        for _ in range(3):',
    '            try:',
    "                subprocess.run(['taskkill', '/F', '/PID', str(parent_pid)],",
    '                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,',
    '                               timeout=20)',
    '            except Exception:',
    '                pass',
    '            if not alive(parent_pid):',
    '                break',
    '            time.sleep(0.4)',
    '        return not alive(parent_pid)',
    '    for sig in (signal.SIGABRT, signal.SIGKILL):',
    '        # SIGABRT first: faulthandler dumps every thread stack into the report',
    '        # even when the interpreter could not run its own watchdog thread.',
    '        try:',
    '            os.kill(parent_pid, sig)',
    '        except Exception:',
    '            pass',
    '        time.sleep(1.0 if sig is signal.SIGABRT else 0.3)',
    '    return wait_gone(parent_pid, 3.0)',
    '',
    '',
    "report('[watchdog] supervisor running: pid %d, watching %d, stall limit %.0fs'",
    '       % (os.getpid(), parent_pid, stall))',
    '',
    'started = time.monotonic()               # this process owns the clock',
    'last_sequence = None',
    'last_progress = time.monotonic()',
    "watching = 'unknown'",
    '',
    'while not gone.is_set():',
    '    time.sleep(0.5)                      # bounded wait; the loop cannot hang',
    '    try:',
    '        if not alive(parent_pid):',
    "            report('[watchdog] pytest process is gone; supervisor standing down')",
    '            break',
    '        if time.monotonic() - started > session_cap:',
    "            report('[watchdog] supervisor: session cap reached, standing down')",
    '            break',
    '        state = read_heartbeat()',
    '        if (state is not None and state[0] == parent_pid',
    '                and state[1] != last_sequence):',
    '            last_sequence = state[1]',
    '            last_progress = time.monotonic()',
    '            watching = state[2]',
    '            continue',
    '        stalled = time.monotonic() - last_progress',
    '        patience = stall',
    '        if hang_already_reported():',
    '            # The in-process watchdog already declared this run dead. Do not sit',
    '            # out the whole guard window: a wedged diagnostic can keep the',
    '            # heartbeat alive for a while, and the run is over either way.',
    '            patience = min(stall, hang_grace)',
    '        if stalled > patience:',
    "            report('')",
    "            report('=' * 78)",
    "            report('[watchdog] NO PROGRESS: no heartbeat for %.0fs' % stalled)",
    "            report('[watchdog] last test started: %s' % watching)",
    "            report('=' * 78)",
    '            if hang_already_reported():',
    "                report(FIRED + '; killing pytest')",
    "                annotate('the in-process watchdog fired but could not finish'",
    "                         ' its own exit (a stuck stack capture?)', watching)",
    '            else:',
    "                report(BLIND + '; killing pytest')",
    "                annotate('no heartbeat for %.0fs (it cannot run)' % stalled,",
    "                         watching)",
    '            killed = kill_pytest()',
    '            report(killed',
    "                   and '[watchdog] pytest killed (non-zero exit); the job must fail'",
    "                   or '[watchdog] pytest could not be killed; the job is stuck')",
    "            if os.name == 'nt':",
    '                # Best effort sweep of nested processes; the parent is already',
    '                # gone, and /T cannot take this supervisor down before it told',
    '                # the report what happened.',
    '                try:',
    "                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent_pid)],",
    '                                   stdout=subprocess.DEVNULL,',
    '                                   stderr=subprocess.DEVNULL, timeout=20)',
    '                except Exception:',
    '                    pass',
    '            break',
    '    except Exception as exc:                 # never die quietly',
    '        try:',
    "            report('[watchdog] supervisor error %r; killing pytest' % (exc,))",
    '            kill_pytest()',
    '        except Exception:',
    '            pass',
    '        break',
    'sys.exit(1)'])


def _start_supervisor() -> None:
    """Starts the supervisor and confirms it is actually watching.

    A supervisor that dies before it starts, or that never gets as far as its
    first line, guards nothing - so say so instead of leaving a silent gap.
    """
    global _supervisor, _supervisor_log
    if GUARD_LIMIT <= 0:
        return
    try:
        _supervisor_log = open(SUPERVISOR_LOG_PATH, "wb")
        stderr_target = _supervisor_log
    except OSError:
        stderr_target = subprocess.DEVNULL
    arguments = [sys.executable, "-c", _SUPERVISOR_SOURCE, HEARTBEAT_PATH,
                 f"{GUARD_LIMIT:.3f}", REPORT_PATH, str(os.getpid()), "5400",
                 f"{HANG_GRACE:.3f}"]
    for stdout_target in (_RAW_OUT, subprocess.DEVNULL):
        try:
            _supervisor = subprocess.Popen(
                arguments, stdin=subprocess.PIPE, stdout=stdout_target,
                stderr=stderr_target, cwd=tempfile.gettempdir(), start_new_session=True,
            )
            break
        except BaseException as exc:
            _supervisor = None
            if stdout_target is subprocess.DEVNULL:
                _say(f"[watchdog] supervisor could not start ({exc!r}); "
                     "the in-process watchdog is the only protection left")
                return
    _say(f"[watchdog] supervisor spawned: pid {_supervisor.pid}, "
         f"stall limit {GUARD_LIMIT:.0f}s")
    deadline = time.monotonic() + SUPERVISOR_HANDSHAKE
    announced = False
    while time.monotonic() < deadline:          # bounded: never waits forever
        if f"supervisor running: pid {_supervisor.pid}" in _read_report():
            announced = True
            break
        if _supervisor.poll() is not None:
            break
        time.sleep(0.05)
    if announced:
        _say(f"[watchdog] supervisor confirmed alive (pid {_supervisor.pid})")
        return
    detail = ""
    if _supervisor.poll() is not None:
        detail = f" (it exited with code {_supervisor.returncode})"
        tail = _supervisor_tail()
        if tail:
            detail += f": {tail}"
        _supervisor = None
    _say(f"[watchdog] supervisor did not confirm it was watching within "
         f"{SUPERVISOR_HANDSHAKE:.0f}s{detail}; "
         "the in-process watchdog is the only protection left")


def _stop_supervisor() -> None:
    global _supervisor, _supervisor_log
    supervisor, _supervisor = _supervisor, None
    log, _supervisor_log = _supervisor_log, None
    if log is not None:
        try:
            log.close()
        except BaseException:
            pass
    if supervisor is None:
        return
    try:
        if supervisor.stdin is not None:
            supervisor.stdin.close()          # EOF: the supervisor stands down
    except BaseException:
        pass
    try:
        supervisor.wait(timeout=5)            # bounded: never wait forever
    except BaseException:
        try:
            supervisor.kill()
        except BaseException:
            pass


def pytest_configure(config):
    """Empties the report once, then starts every watchdog."""
    global _report, _heartbeat, _REPORT_FD
    try:
        with open(REPORT_PATH, "w", encoding="utf-8"):
            pass                              # only this truncates; all writers append
    except OSError:
        pass
    _report = None                            # reopen in append mode, never "w"
    try:
        _REPORT_FD = os.open(REPORT_PATH,
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        _REPORT_FD = None                     # the guard falls back to the supervisor
    _capture_real_streams(config)
    _say(f"[watchdog] session start: {TEST_LIMIT:.0f}s per test "
         f"({DUMP_GRACE:.0f}s diagnostic window), {GUARD_LIMIT:.0f}s heartbeat limit, "
         f"heartbeat every {HEARTBEAT_INTERVAL:.1f}s, report {REPORT_PATH}")
    _write_heartbeat("session-start")
    if GUARD_LIMIT > 0 and HEARTBEAT_INTERVAL > 0:
        _heartbeat = _Heartbeat(HEARTBEAT_INTERVAL)
        _heartbeat.start()
    _start_supervisor()


def pytest_sessionstart(session):
    """Points crash dumps at the report, after every plugin had its say.

    pytest's own faulthandler plugin enables dumping in `pytest_configure` with
    `trylast=True`, so it would otherwise take the target back for stderr - which
    pytest redirects into a captured file that a fatal exit never prints. Doing it
    here (after configure, before the first test) keeps every dump - the
    in-process watchdog's, a native crash, the supervisor's SIGABRT - in the one
    file the CI prints.
    """
    try:
        faulthandler.enable(file=_report_file(), all_threads=True)
    except BaseException as exc:
        _say(f"[watchdog] could not arm crash dumps: {exc!r}")


def pytest_collection_finish(session):
    _write_heartbeat("collection-finished")


def pytest_runtest_logstart(nodeid, location):
    """Arms the watchdogs for one test, before its fixtures run."""
    global _watchdog
    _mark_test_started(nodeid)
    _write_heartbeat(nodeid, 0.0)
    if TEST_LIMIT <= 0:
        return
    _say(f"=== {nodeid} (limit {TEST_LIMIT:.0f}s)", loud=False)
    _watchdog = _TestWatchdog(nodeid, TEST_LIMIT, TEST_NOTICE)
    _watchdog.start()


def pytest_runtest_logfinish(nodeid, location):
    """Disarms the watchdogs: the test (and its teardown) finished in time."""
    global _watchdog
    watchdog, _watchdog = _watchdog, None
    if watchdog is not None:
        watchdog.finished()
        watchdog.join(timeout=2.0)            # bounded: a stuck thread is a daemon
    _mark_test_finished(nodeid)
    _write_heartbeat("between tests")


def pytest_unconfigure(config):
    global _heartbeat
    if _heartbeat is not None:
        _heartbeat.stop()
        _heartbeat = None
    _stop_supervisor()
    _say("[watchdog] session finished")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(
        f"Watchdogs: {TEST_LIMIT:.0f}s per test (exit {WATCHDOG_EXIT_CODE}), "
        f"{GUARD_LIMIT:.0f}s heartbeat limit, report in {REPORT_PATH}"
    )
