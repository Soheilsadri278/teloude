# teloude/tests/test_hang_watchdog.py
"""Proofs that the hang watchdogs really terminate a blocked run, promptly.

These run real pytest sessions against deliberately blocked test files and check
the outcome: terminated within the bound, the exact node id in the report, every
thread stack dumped, a GitHub annotation naming the test, non-zero exit status, no
lingering watchdog process. They are the reason a future edit cannot quietly break
the safety net. Three real defects are pinned here, each found in a failing run:

* a supervisor program that did not compile, so it guarded nothing;
* a report opened without O_APPEND, so a healthy supervisor's own line was
  overwritten and the watchdog only *looked* dead;
* an in-process dump that could block, so the watchdog's own exit never ran and
  the job died 15s later with exit code 1 and no explanation.

Nothing here is skipped or weakened: a watchdog that cannot terminate a
deliberate deadlock is a broken watchdog, not a flaky test.
"""
import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
#: The probe's pytest node id, as pytest reports it (path relative to the rootdir).
PROBE_NODEID = "teloude/tests/test_zz_watchdog_probe.py"
REPO_ROOT = TESTS_DIR.parents[1]

#: The supervisor's own announcement, complete: proof the second writer's line is
#: still intact after the first writer appended more lines to the same file.
SUPERVISOR_LINE = re.compile(
    r"^\[watchdog\] supervisor running: pid \d+, watching \d+, stall limit \d+s$",
    re.MULTILINE,
)

DEADLOCK_SOURCE = """import threading


def test_a_controlled_deadlock():
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()          # never returns: the watchdog has to end this run
    raise AssertionError("unreachable")
"""

BLOCKED_DIAGNOSTIC_SOURCE = """import faulthandler
import threading
import time


def _never_returns(*args, **kwargs):
    time.sleep(600)         # a blocked diagnostic must not keep the job alive


# a stack capture that blocks, the way faulthandler's can on Windows
faulthandler.dump_traceback = _never_returns


def test_a_controlled_deadlock():
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()          # never returns
"""

#: The same, but the diagnostic blocks *while holding the GIL*: an extension or a
#: C library that never releases it. `ctypes.PyDLL` is the portable way to make
#: such a call from Python - it deliberately does not release the GIL - so no
#: Python thread of this process can be scheduled afterwards, which is the worst
#: case a watchdog can face. It borrows `kernel32.Sleep` on Windows (exported by
#: every version) and libc `sleep` elsewhere.
GIL_BLOCKING_DIAGNOSTIC_SOURCE = """import ctypes
import faulthandler
import os
import threading

if os.name == "nt":
    def _hold_the_gil():
        ctypes.PyDLL("kernel32").Sleep(600000)     # milliseconds, GIL held
else:
    def _hold_the_gil():
        ctypes.PyDLL(None).sleep(600)              # seconds, GIL held


def _a_diagnostic_that_holds_the_gil(*args, **kwargs):
    # A PyDLL call keeps the GIL for its whole duration: from here on nothing in
    # this process can run, Python thread or not.
    _hold_the_gil()


faulthandler.dump_traceback = _a_diagnostic_that_holds_the_gil


def test_a_controlled_deadlock():
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()          # never returns
"""

#: Arms the C-level guard itself, then holds the GIL: the mechanism proof, with no
#: plugin, no pytest and no Python thread able to help.
NATIVE_GUARD_SCRIPT = """import ctypes
import faulthandler
import os
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
faulthandler.dump_traceback_later(float(sys.argv[2]), repeat=False,
                                  file=descriptor, exit=True)
sys.stdout.write("armed\\n")
sys.stdout.flush()
if os.name == "nt":
    ctypes.PyDLL("kernel32").Sleep(600000)     # holds the GIL, like a C library
else:
    ctypes.PyDLL(None).sleep(600)              # holds the GIL, like a C library
"""

SILENT_PROGRESS_SOURCE = """import time


def test_a_block_the_python_watchdog_cannot_see():
    time.sleep(600)         # the interpreter cannot report progress any more
"""

SLOW_BUT_ALIVE_SOURCE = """import time


def test_slow_but_demonstrably_alive():
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.1)     # working, not wedged: the heartbeat keeps moving
"""

STOPPED_SOURCE = """import os
import signal


def test_that_stops_itself():
    os.kill(os.getpid(), signal.SIGSTOP)   # the whole process stops: no heartbeat
"""

PASSING_SOURCE = """def test_that_finishes_immediately():
    assert True
"""


def _plugin():
    """The watchdog plugin module, as the suite itself loads it."""
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    import watchdog_plugin

    return watchdog_plugin


def _alive(pid: int) -> bool:
    """True while a process with this id exists (Windows and POSIX)."""
    if os.name == "nt":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True)
        return str(pid) in listing.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, timeout: float = 15.0) -> bool:
    """Waits, with a deadline, for a helper process to disappear."""
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    return not _alive(pid)


def _wait_for_text(path: Path, needle: str, timeout: float = 15.0) -> str:
    """Waits, with a deadline, for a line to appear; returns the file contents.

    The supervisor announces itself, then kills, then confirms - a few tenths of
    a second after the pytest process it killed is already gone. Waiting for the
    confirmation is what makes these checks deterministic instead of racy.
    """
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if needle in text:
            return text
        time.sleep(0.2)
    return text


def _run_pytest_on(tmp_path, source, **env_overrides):
    """Runs a real pytest session on a blocked test file and reports the outcome.

    The probe lives in `teloude/tests` on purpose: that is where the watchdogs are
    wired in for CI, through the conftest, so this exercises the wiring the job
    actually uses - not just the plugin loaded by hand with `-p`. The child's
    stdout is captured too: that is where the GitHub annotation is written.
    """
    probe = TESTS_DIR / "test_zz_watchdog_probe.py"
    probe.write_text(source, encoding="utf-8")
    report = tmp_path / "watchdog.log"
    env = dict(os.environ)
    env.update({
        "TELOUDE_WATCHDOG_FILE": str(report),
        # the plugin is loaded by module name, so its directory must be importable
        "PYTHONPATH": os.pathsep.join([str(TESTS_DIR), str(tmp_path)]),
        "PYTHONUNBUFFERED": "1",
    })
    env.update({name: str(value) for name, value in env_overrides.items()})
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
             str(probe)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
    finally:
        probe.unlink(missing_ok=True)
    elapsed = time.monotonic() - started
    text = report.read_text(encoding="utf-8", errors="replace") if report.exists() else ""
    return result, text, elapsed, report


def _supervisor_pid(report: str) -> int:
    """The pid the session spawned; the line is written before the handshake."""
    match = re.search(r"supervisor spawned: pid (\d+)", report)
    assert match, f"no supervisor was started for this session:\n{report}"
    return int(match.group(1))


def _annotation(text: str, nodeid: str) -> str:
    """The GitHub annotation naming this test, or an empty string."""
    match = re.search(rf"^::error title=Hang watchdog::{re.escape(nodeid)} .*$",
                      text, re.MULTILINE)
    return match.group(0) if match else ""


def test_the_embedded_supervisor_is_valid_python():
    """The supervisor is a program inside this file: it must actually compile."""
    plugin = _plugin()
    source = plugin._SUPERVISOR_SOURCE
    ast.parse(source)                             # SyntaxError = no protection
    assert "kill_pytest()" in source
    assert "sys.exit(1)" in source, "a killed run must not look successful"
    assert "taskkill" in source, "Windows needs a kill it can run"
    # Progress is a heartbeat sequence timed by the supervisor's own clock; the
    # mtime-versus-wall-clock design killed live runs when the two disagreed.
    assert "time.monotonic" in source, "the supervisor must own its clock"
    assert "st_mtime" not in source, "progress must not be read from file mtime"
    assert "read_heartbeat" in source and "hang_already_reported" in source
    # A HANG on record shortens the wait, and only lines from this session count.
    assert "patience = min(stall, hang_grace)" in source
    assert "handle.seek(hang_offset)" in source, (
        "a stale HANG line from an earlier run must not shorten this one"
    )
    assert "sys.exit(1)" in source


def test_the_termination_bound_fits_inside_the_supervisor_window():
    """The documented bound must stay short, and this is where it is defined.

    Two numbers, both true: the in-process watchdog ends a hang within
    `TEST_LIMIT + max(DUMP_GRACE, HANG_GRACE)`, and the supervisor is the
    guarantee that does not need anything inside the test process - so the worst
    case is `TEST_LIMIT + GUARD_LIMIT`. Nothing here may outgrow the supervisor.
    """
    plugin = _plugin()
    assert 0 < plugin.TEST_LIMIT <= 60, "the per-test limit must stay short"
    assert 0 <= plugin.DUMP_GRACE <= 10, "the diagnostic window must stay short"
    assert plugin.DUMP_GRACE <= plugin.HANG_GRACE, (
        "the supervisor's short patience must not fire before the diagnostic window"
    )
    assert plugin.HANG_GRACE < plugin.GUARD_LIMIT, (
        "a HANG on record must shorten the wait, not extend it"
    )
    assert plugin.GUARD_LIMIT >= plugin.TEST_LIMIT, "the supervisor is the backstop"
    in_process = plugin.TEST_LIMIT + max(plugin.DUMP_GRACE, plugin.HANG_GRACE)
    worst_case = plugin.TEST_LIMIT + plugin.GUARD_LIMIT
    assert in_process < worst_case, "the guard must be the common case, not the backstop"
    assert worst_case <= 90, "the worst case must stay inside the CI job budget"
    assert plugin.WATCHDOG_EXIT_CODE != 0
    assert plugin.WATCHDOG_EXIT_CODE not in range(0, 6), "not a pytest exit status"


def test_the_termination_path_takes_no_lock_and_arms_the_guard_first():
    """The order inside `_bail` is the guarantee, so it is pinned here too."""
    plugin = _plugin()
    source = Path(plugin.__file__).read_text(encoding="utf-8")
    bail = source.split("def _bail(self)", 1)[1].split("def _arm_native_guard", 1)[0]

    assert "_TERMINATING = True" in bail, "the heartbeat must learn about it first"
    guard = bail.index("self._arm_native_guard()")
    for later in ("_emergency_write(", "_annotate_error(", "self._finished.set()",
                  "_dump_stacks()", "_terminate("):
        assert guard < bail.index(later), (
            f"the guard must be armed before {later}"
        )
    assert "_say(" not in bail, "the termination path must not wait for _report_lock"
    # and nothing else may wait for that lock without a deadline either
    assert "_report_lock.acquire(timeout=SAY_LOCK_TIMEOUT)" in source, (
        "a wedged report writer must not be able to stop the session"
    )
    say = source.split("def _say(", 1)[1].split("\ndef ", 1)[0]
    assert "_TERMINATING" in say, (
        "a diagnostic printed while the run is ending must not wait for the lock"
    )
    assert "_emergency_write(message)" in source, "the fallback write must exist"
    terminate = source.split("def _terminate(self", 1)[1].split("\ndef ", 1)[0]
    assert "_say(" not in terminate, "the final line must not wait for the lock"
    # and the guard is the C-level exit, not a Python thread that needs the GIL
    arm = source.split("def _arm_native_guard(self", 1)[1].split("\ndef ", 1)[0]
    assert "faulthandler.dump_traceback_later(" in arm
    assert "exit=True" in arm
    assert "threading.Thread" not in arm, "the guard must not depend on a Python thread"


def test_the_suite_conftest_registers_every_watchdog_hook():
    """CI loads the watchdogs through the conftest: every hook must be wired.

    An import list that misses one hook leaves a real hole - a missing
    `pytest_sessionstart` once meant crash dumps were armed by faulthandler but
    immediately overridden, so a killed run reported no stacks.
    """
    conftest = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    plugin = _plugin()
    hooks = [
        name for name in dir(plugin)
        if name.startswith("pytest_") and callable(getattr(plugin, name))
    ]
    assert hooks, "the plugin defines no hooks at all"
    missing = [name for name in hooks if name not in conftest]
    assert not missing, f"teloude/tests/conftest.py does not register {missing}"


def test_the_windows_kill_path_targets_pytest_and_only_pytest():
    """The kill must be aimed, checked and reported - not a blind sweep."""
    source = _plugin()._SUPERVISOR_SOURCE
    assert "taskkill', '/F', '/PID', str(parent_pid)" in source, (
        "Windows must kill exactly the pytest process by pid"
    )
    primary = source.split("def kill_pytest():", 1)[1].split("        return not alive")[0]
    assert "/T" not in primary, (
        "the pid-targeted kill must run first; a tree kill cannot take this "
        "supervisor down before it has reported"
    )
    assert "if not alive(parent_pid):" in primary, "never signal a recycled pid"
    assert "wait_gone(parent_pid, 3.0)" in source, "the kill must be confirmed"


def test_a_deadlocked_test_is_terminated_with_a_report(tmp_path):
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, DEADLOCK_SOURCE, TELOUDE_TEST_WATCHDOG="2", TELOUDE_TEST_GUARD="20"
    )

    assert result.returncode != 0, "a hang must fail the run"
    assert result.returncode == _plugin().WATCHDOG_EXIT_CODE, result.stdout + result.stderr
    assert elapsed < 30, f"the watchdog took {elapsed:.1f}s to act"
    # The report must name the test and show where it is stuck.
    assert "HANG" in report
    assert "test_zz_watchdog_probe.py" in report
    assert "in test_a_controlled_deadlock" in report, "the blocked frame must be dumped"
    assert "terminating pytest with exit code" in report
    # Both writers share the report: the supervisor's line must survive the lines
    # this process appended after it (a non-append handle silently overwrites it).
    assert SUPERVISOR_LINE.search(report), f"the supervisor line was lost:\n{report}"
    assert "supervisor confirmed alive (pid" in report, "the handshake must be recorded"
    # The annotation names the test and the reason, in the report and in the job log.
    assert _annotation(report, "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")
    assert _annotation(result.stdout, "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")
    assert "ran longer than 2s" in _annotation(result.stdout,
                                               "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")


#: What the C-level guard exits with: faulthandler calls `_exit(1)` itself.
NATIVE_GUARD_EXIT_CODE = 1


def test_a_blocked_diagnostic_cannot_prevent_bounded_termination(tmp_path):
    """Termination outranks diagnostics: a stuck stack capture must not stall CI.

    The probe replaces `faulthandler.dump_traceback` with something that never
    returns, which is what a Windows stack capture can do when the thread it reads
    holds a runtime lock. The Python path stops in that call, so the C-level guard
    armed before it is what ends the run - inside the diagnostic window, with the
    node id, the reason and the blocked frame already on record.
    """
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, BLOCKED_DIAGNOSTIC_SOURCE, TELOUDE_TEST_WATCHDOG="2",
        TELOUDE_TEST_DUMP_GRACE="1", TELOUDE_TEST_GUARD="30",
    )

    assert result.returncode == NATIVE_GUARD_EXIT_CODE, (
        f"the C-level guard must be the one that ends this run, got "
        f"{result.returncode}:\n{report}"
    )
    assert elapsed < 15, f"a blocked diagnostic held the run for {elapsed:.1f}s"
    assert "HANG: " in report and "ran longer than 2s" in report
    assert "native guard armed" in report, "the guard must be armed before the dump"
    assert "in test_a_controlled_deadlock" in report, (
        f"the blocked frame must be on record even when the dump blocks:\n{report}"
    )
    assert "terminating pytest with exit code" not in report, (
        "the blocked dump must not be waited for"
    )
    assert _annotation(result.stdout, "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")


def test_a_diagnostic_that_holds_the_gil_still_terminates_within_the_bound(tmp_path):
    """The bound must hold when no Python thread of the process can run at all.

    The probe's diagnostic holds the GIL through a C call, so the heartbeat thread,
    the watchdog thread and the main thread are all frozen the moment the dump is
    attempted. Only something that needs neither the GIL nor a Python thread can
    end this run - which is what the C-level guard does, within `DUMP_GRACE`.
    """
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, GIL_BLOCKING_DIAGNOSTIC_SOURCE, TELOUDE_TEST_WATCHDOG="2",
        TELOUDE_TEST_DUMP_GRACE="1", TELOUDE_TEST_GUARD="30",
    )

    assert result.returncode == NATIVE_GUARD_EXIT_CODE, (
        f"a GIL-holding diagnostic must still end the run through the C-level "
        f"guard, got {result.returncode}:\n{report}"
    )
    assert elapsed < 15, f"a GIL-holding diagnostic held the run for {elapsed:.1f}s"
    assert "HANG: " in report and "native guard armed" in report
    assert "in test_a_controlled_deadlock" in report, (
        f"the blocked frame must be on record before the GIL is taken:\n{report}"
    )
    assert "terminating pytest with exit code" not in report, (
        "no Python code can run after the diagnostic takes the GIL; the guard ends it"
    )
    assert _annotation(result.stdout, "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")


def test_the_native_guard_exits_while_the_gil_is_held(tmp_path):
    """The guard mechanism itself, with no plugin and no pytest in the way.

    This runs the same interpreter the tests use (Python 3.12 in CI) and holds the
    GIL in a C call after arming: the process must still end, by itself, with the
    blocked frame in the report. If this ever regresses, the in-process bound is a
    promise the code cannot keep.
    """
    report = tmp_path / "guard.log"
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", NATIVE_GUARD_SCRIPT, str(report), "1"],
        capture_output=True, text=True, timeout=60,
    )
    elapsed = time.monotonic() - started
    text = report.read_text(encoding="utf-8", errors="replace") if report.exists() else ""

    assert result.returncode == NATIVE_GUARD_EXIT_CODE, result.stdout + result.stderr
    assert elapsed < 10, f"the guard took {elapsed:.1f}s while the GIL was held"
    assert "armed" in result.stdout, "the guard must have been armed before the block"
    assert "Timeout (" in text, f"the guard must report the timeout it hit:\n{text}"
    assert "in <module>" in text, (
        f"the guard must name the frame that held the GIL:\n{text}"
    )


def test_a_wedged_report_writer_cannot_block_termination(tmp_path):
    """Another thread holding `_report_lock` forever must not stop the session.

    The lock only keeps report lines from interleaving, so nothing may wait for it
    without a deadline: `_say()` falls back to a raw `os.write()`, the termination
    path never takes it at all, and the guard ends the run regardless. The probe
    parks a thread inside the lock for the whole session; the run must still start,
    still hang, still be terminated, and still name the test.
    """
    lock_holder = tmp_path / "hold_report_lock.py"
    lock_holder.write_text(
        "import threading\n"
        "import time\n"
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_sessionstart(session):\n"
        "    import watchdog_plugin as wp\n"
        "\n"
        "    def hold():\n"
        "        wp._report_lock.acquire()\n"
        "        time.sleep(600)          # the lock is never released\n"
        "\n"
        "    threading.Thread(target=hold, name='stuck-report-writer',\n"
        "                     daemon=True).start()\n",
        encoding="utf-8",
    )
    probe = TESTS_DIR / "test_zz_watchdog_probe.py"
    probe.write_text(DEADLOCK_SOURCE, encoding="utf-8")
    report = tmp_path / "watchdog.log"
    env = dict(os.environ)
    env.update({
        "TELOUDE_WATCHDOG_FILE": str(report),
        # the plugin is loaded by module name, so its directory must be importable
        "PYTHONPATH": os.pathsep.join([str(TESTS_DIR), str(tmp_path)]),
        "PYTHONUNBUFFERED": "1",
        "TELOUDE_TEST_WATCHDOG": "2",
        "TELOUDE_TEST_DUMP_GRACE": "1",
        "TELOUDE_TEST_GUARD": "30",
        "TELOUDE_TEST_SAY_TIMEOUT": "0.2",     # do not spend the lock timeout per line
    })
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "-p", lock_holder.stem, "-q", str(probe)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
    finally:
        probe.unlink(missing_ok=True)
    elapsed = time.monotonic() - started
    text = report.read_text(encoding="utf-8", errors="replace") if report.exists() else ""

    # Either in-process mechanism may be the one that lands, and both are correct:
    # no diagnostic waits for the wedged lock any more, so the Python path exits 97 -
    # but if the stack capture itself blocked (a Windows suspension, the residual
    # risk the guard exists for) the guard exits 1 inside the diagnostic window.
    # What matters is that the run ends, promptly, and says why.
    assert result.returncode in (NATIVE_GUARD_EXIT_CODE, _plugin().WATCHDOG_EXIT_CODE), (
        f"a wedged report writer must not be able to keep the run alive, got "
        f"{result.returncode}:\n{text}"
    )
    assert elapsed < 15, f"a wedged report writer held the run for {elapsed:.1f}s"
    assert "session start" in text, (
        f"the session must start even though the lock never comes back:\n{text}"
    )
    assert "HANG: " in text, (
        f"the verdict must bypass `_report_lock` and still reach the report:\n{text}"
    )
    assert "test_zz_watchdog_probe.py::test_a_controlled_deadlock" in text
    assert "native guard armed" in text
    assert _annotation(result.stdout, "teloude/tests/test_zz_watchdog_probe.py::test_a_controlled_deadlock")


def test_a_wedged_collection_is_killed_by_the_supervisor(tmp_path):
    """The heartbeat must not vouch for a session that is wedged outside a test.

    A helper thread that can still run is not evidence that pytest is healthy: a
    session stuck in collection, in a report write or in a diagnostic would then
    borrow the heartbeat's credibility and hang until the job timeout. So the
    heartbeat says nothing at all when no test is in flight, and the supervisor -
    which needs no cooperation from this process - acts on its own clock. The probe
    blocks while its module is being imported, i.e. during collection, before any
    test exists.
    """
    result, report, elapsed, path = _run_pytest_on(
        tmp_path, "import time\n\ntime.sleep(600)   # wedged while collecting\n",
        TELOUDE_TEST_WATCHDOG="0", TELOUDE_TEST_GUARD="4",
    )

    assert result.returncode != 0, "a wedged collection must fail the run"
    assert elapsed < 20, f"the supervisor took {elapsed:.1f}s to act"
    report = _wait_for_text(path, "pytest killed")
    assert "supervisor confirmed alive (pid" in report
    assert "NO PROGRESS" in report
    assert "last test started: session-start" in report, (
        f"the report must say where the session stopped:\n{report}"
    )
    assert "pytest killed" in report


def test_a_wedged_diagnostic_cannot_keep_the_supervisor_asleep(tmp_path):
    """With the C-level guard out of the picture, the supervisor must still act.

    The heartbeat exists to spare slow tests, and the price is that it can also
    vouch for a run that is already over. So it stops the moment the watchdog
    fires, and the supervisor - seeing a stuck run that has a HANG on record -
    cuts its patience to `HANG_GRACE` instead of waiting out the guard window.
    This is the last line of defence when nothing inside the process can act.
    """
    result, report, elapsed, path = _run_pytest_on(
        tmp_path, GIL_BLOCKING_DIAGNOSTIC_SOURCE, TELOUDE_TEST_WATCHDOG="2",
        TELOUDE_TEST_DUMP_GRACE="0", TELOUDE_TEST_GUARD="30",
    )

    assert result.returncode != 0, "the supervisor must fail the run"
    assert elapsed < 20, f"the supervisor took {elapsed:.1f}s to act"
    report = _wait_for_text(path, "killing pytest")
    assert "NO PROGRESS" in report
    assert "the in-process watchdog fired but its exit is blocked" in report, (
        f"the supervisor must see the HANG and stop trusting the heartbeat:\n{report}"
    )
    assert "test_zz_watchdog_probe.py::test_a_controlled_deadlock" in report


def test_the_supervisor_kills_a_run_that_cannot_report_progress(tmp_path):
    """With no heartbeat at all, the separate process must end the run."""
    result, report, elapsed, path = _run_pytest_on(
        tmp_path, SILENT_PROGRESS_SOURCE, TELOUDE_TEST_WATCHDOG="0",
        TELOUDE_TEST_HEARTBEAT="0", TELOUDE_TEST_GUARD="3",
    )

    assert result.returncode != 0, "the supervisor must fail the run"
    assert elapsed < 30, f"the supervisor took {elapsed:.1f}s to act"
    assert "supervisor confirmed alive (pid" in report, "the supervisor must prove it lives"
    assert "NO PROGRESS" in report
    nodeid = f"{PROBE_NODEID}::test_a_block_the_python_watchdog_cannot_see"
    assert nodeid in report, f"the report must name the stranded test:\n{report}"
    # The confirmation is written a moment after the process it killed is gone.
    report = _wait_for_text(path, "pytest killed")
    assert "pytest killed" in report
    # Even with the in-process watchdog off, the killed run must dump its stacks:
    # faulthandler gets SIGABRT before the supervisor escalates to SIGKILL.
    assert "in test_a_block_the_python_watchdog_cannot_see" in report, (
        f"the victim's stacks were not dumped:\n{report}"
    )
    assert _annotation(report, nodeid), "the supervisor must annotate the stranded test"
    assert _annotation(result.stdout, nodeid), (
        f"the annotation must reach the job log, not only the report:\n{result.stdout}"
    )


def test_a_slow_test_that_keeps_progressing_is_not_killed(tmp_path):
    """Fast enough is not the question: progress is. Slow work stays alive."""
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, SLOW_BUT_ALIVE_SOURCE, TELOUDE_TEST_WATCHDOG="30",
        TELOUDE_TEST_GUARD="4",
    )

    assert result.returncode == 0, f"a progressing test was killed:\n{report}"
    assert elapsed >= 7, "the probe must really have run for its full time"
    assert "NO PROGRESS" not in report, (
        "the heartbeat must keep a live, progressing run alive"
    )
    assert "1 passed" in result.stdout, result.stdout


def test_a_stopped_process_is_killed_promptly(tmp_path):
    """A process that cannot run at all - the case the in-process layer cannot see.

    `SIGSTOP` freezes the whole pytest process: no thread of it can write a
    heartbeat, exactly like a block in native code. Windows has no equivalent, so
    this is POSIX-only; the portable proof of the same path is the no-heartbeat
    test above.
    """
    if os.name == "nt":
        return None
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, STOPPED_SOURCE, TELOUDE_TEST_WATCHDOG="0", TELOUDE_TEST_GUARD="3",
    )

    assert result.returncode != 0, "a stopped run must not be left behind"
    assert elapsed < 30, f"the supervisor took {elapsed:.1f}s to act"
    assert "NO PROGRESS" in report
    assert "teloude/tests/test_zz_watchdog_probe.py::test_that_stops_itself" in report
    assert "pytest killed" in _wait_for_text(_path_of(tmp_path), "pytest killed")


def _path_of(tmp_path) -> Path:
    return tmp_path / "watchdog.log"


def test_no_watchdog_process_survives_a_run(tmp_path):
    """The watchdogs are helpers: they must not outlive the session they guard."""
    _result, report, _elapsed, path = _run_pytest_on(
        tmp_path, PASSING_SOURCE, TELOUDE_TEST_WATCHDOG="0", TELOUDE_TEST_GUARD="30"
    )
    assert "session start" in report
    # The supervisor is starting up while the test runs; wait for its line with a
    # deadline instead of assuming it is already on disk.
    report = _wait_for_text(path, "supervisor spawned")
    pid = _supervisor_pid(report)
    assert _wait_until_gone(pid, timeout=10.0), (
        "the supervisor outlived the pytest session it guarded"
    )
