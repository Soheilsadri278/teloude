# teloude/tests/watchdog_plugin.py
"""Watchdogs: a test that blocks must fail the job in seconds, never hold it.

A blocked test once held the whole CI job: the runner sat there with no output
until somebody cancelled it, and nothing said which test was waiting. Two
independent watchdogs cover that now, and neither of them can wait forever.

1. In-process watchdog, one per test (`_TestWatchdog`). A daemon thread counts
   the wall-clock time of the test that is running. After
   `TELOUDE_TEST_WATCHDOG` seconds it writes the pytest node id, dumps the stack
   of EVERY thread with faulthandler and terminates the interpreter with exit
   code 97, so the job fails at once instead of waiting. Its loop only ever waits
   with a deadline, so the watchdog itself cannot hang.

2. Supervisor process, one per session (`_SUPERVISOR_SOURCE`). A separate
   interpreter, started with the session, watches a heartbeat file that the
   pytest process rewrites for every test. If the heartbeat goes stale for
   `TELOUDE_TEST_GUARD` seconds - the case where the in-process watchdog could
   not run at all, because the interpreter is stuck in native code or because
   somebody disabled it - the supervisor records which test was running, kills
   the pytest process (`taskkill /F` on Windows, SIGABRT then SIGKILL elsewhere)
   and exits with a non-zero status. Being a different process, it cannot be
   blocked by whatever blocks pytest. It also stands down on its own: as soon as
   the pytest process is gone, or its stdin reaches EOF, it exits - a watchdog
   must never outlive the run it guards, and it must never signal a pid that has
   been recycled in the meantime.

Both report into one file (`TELOUDE_WATCHDOG_FILE`), which the CI step prints
with `if: always()`, so a hang is explained even though pytest never reached its
own summary. Crash dumps go there too (`faulthandler.enable(file=...)`), so a
Windows access violation or the supervisor's SIGABRT names the Python frames.

Both writers open that report for APPENDING, and only the session start truncates
it. That is not a detail: a handle opened without O_APPEND keeps its own idea of
the file offset and silently overwrites whatever the other writer appended in the
meantime. That is exactly how a healthy supervisor's "supervisor running" line
once vanished and made a working watchdog look dead - so `_start_supervisor`
also insists on a handshake (the supervisor must announce itself in the report
within `SUPERVISOR_HANDSHAKE` seconds) and says so in the report and on stderr
when it does not, instead of leaving a quiet watchdog behind.

Configuration, in seconds (0 turns that watchdog off):

    TELOUDE_TEST_WATCHDOG   per-test limit        default: 30
    TELOUDE_TEST_NOTICE     "still running" line  default: 15
    TELOUDE_TEST_GUARD      stale-heartbeat limit default: per-test limit + 15
    TELOUDE_WATCHDOG_FILE   report path           default: <temp dir>/teloude-pytest-watchdog.log

The numbers are not arbitrary: the slowest test in this suite takes ~6s on a
developer machine (`pytest --durations`), so 30s detects a hang with room for a
CI runner several times slower instead of hiding slowness. A test that trips the
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
REPORT_PATH = os.environ.get("TELOUDE_WATCHDOG_FILE") or os.path.join(
    tempfile.gettempdir(), "teloude-pytest-watchdog.log"
)
HEARTBEAT_PATH = REPORT_PATH + ".heartbeat"
SUPERVISOR_LOG_PATH = REPORT_PATH + ".supervisor.err"

_report = None
_watchdog = None
_supervisor = None
_supervisor_log = None


def _report_file():
    """The one report every watchdog writes to: a real file, never captured.

    Opened for appending, so the supervisor (a different process, same file) and
    this process can never overwrite each other's lines.
    """
    global _report
    if _report is None:
        _report = open(REPORT_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    return _report


def _say(line: str, loud: bool = True) -> None:
    """Records a line in the report and, when it matters, on the raw stderr fd.

    The report is the reliable sink: it is a real file, so neither pytest's
    output capture nor a sudden exit can lose it. The stderr copy is what makes a
    hang visible in the job log while it happens.
    """
    message = line.rstrip()
    try:
        stream = _report_file()
        stream.write(message + "\n")
        stream.flush()
    except BaseException:
        pass
    if not loud:
        return
    try:
        os.write(2, (message + "\n").encode("utf-8", "replace"))
    except BaseException:
        pass


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

    def _bail(self) -> None:
        _say("")
        _say("=" * 78)
        _say(f"[watchdog] HANG: {self._nodeid} ran longer than {self._limit:.0f}s")
        _say("=" * 78)
        try:
            faulthandler.dump_traceback(all_threads=True, file=_report_file())
        except BaseException as exc:
            _say(f"[watchdog] could not dump thread stacks: {exc!r}")
        _say(f"[watchdog] terminating pytest with exit code {WATCHDOG_EXIT_CODE}")
        try:
            _report_file().flush()
        except BaseException:
            pass
        os._exit(WATCHDOG_EXIT_CODE)


def _touch_heartbeat(what: str) -> None:
    """Marks progress for the supervisor (and records what was running)."""
    if GUARD_LIMIT <= 0:
        return
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{what}\n{time.time():.3f}\n")
    except OSError:
        pass


# The supervisor runs in its own interpreter: no lock, no GIL and no Qt object in
# the pytest process can block it. Every loop is bounded, a failure inside it
# still ends with a kill attempt, and it stands down when the session ends, when
# it loses its parent or after the absolute cap (90 minutes).
_SUPERVISOR_SOURCE = "\n".join([
    'import os, signal, subprocess, sys, threading, time',
    '',
    'heartbeat, stall, report_path, parent_pid, session_started, session_cap = (',
    '    sys.argv[1], float(sys.argv[2]), sys.argv[3], int(sys.argv[4]),',
    '    float(sys.argv[5]), float(sys.argv[6]),',
    ')',
    'gone = threading.Event()',
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
    '    if os.name == \'nt\':',
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
    '    deadline = time.time() + timeout',
    '    while time.time() < deadline:',
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
    'def last_test():',
    '    try:',
    "        with open(heartbeat, encoding='utf-8', errors='replace') as handle:",
    "            return (handle.readline() or '').strip() or 'unknown'",
    '    except Exception:',
    "        return 'unknown'",
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
    'while not gone.is_set():',
    '    time.sleep(0.5)                          # bounded wait; the loop cannot hang',
    '    try:',
    '        if not alive(parent_pid):',
    "            report('[watchdog] pytest process is gone; supervisor standing down')",
    '            break',
    '        now = time.time()',
    '        if now - session_started > session_cap:',
    "            report('[watchdog] supervisor: session cap reached, standing down')",
    '            break',
    '        try:',
    '            last = os.stat(heartbeat).st_mtime',
    '        except OSError:',
    '            last = session_started',
    '        if now - last > stall:',
    '            stuck, name = now - last, last_test()',
    "            report('')",
    "            report('=' * 78)",
    "            report('[watchdog] NO PROGRESS: no test heartbeat for %.0fs' % stuck)",
    "            report('[watchdog] last test started: %s' % name)",
    "            report('=' * 78)",
    "            report('[watchdog] in-process watchdog silent; killing pytest')",
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
    try:
        _supervisor = subprocess.Popen(
            [sys.executable, "-c", _SUPERVISOR_SOURCE, HEARTBEAT_PATH,
             f"{GUARD_LIMIT:.3f}", REPORT_PATH, str(os.getpid()),
             f"{time.time():.3f}", "5400"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_target,
            cwd=tempfile.gettempdir(), start_new_session=True,
        )
    except BaseException as exc:
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
    """Empties the report once, then starts both watchdogs."""
    try:
        with open(REPORT_PATH, "w", encoding="utf-8"):
            pass                              # only this truncates; all writers append
    except OSError:
        pass
    _report = None                            # reopen in append mode, never "w"
    _say(f"[watchdog] session start: {TEST_LIMIT:.0f}s per test, "
         f"{GUARD_LIMIT:.0f}s supervisor limit, report {REPORT_PATH}")
    _touch_heartbeat("session-start")
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
    _touch_heartbeat("collection-finished")


def pytest_runtest_logstart(nodeid, location):
    """Arms the watchdogs for one test, before its fixtures run."""
    global _watchdog
    _touch_heartbeat(nodeid)
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
    _touch_heartbeat(f"{nodeid} finished")


def pytest_unconfigure(config):
    _stop_supervisor()
    _say("[watchdog] session finished")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(
        f"Watchdogs: {TEST_LIMIT:.0f}s per test (exit {WATCHDOG_EXIT_CODE}), "
        f"{GUARD_LIMIT:.0f}s supervisor limit, report in {REPORT_PATH}"
    )
