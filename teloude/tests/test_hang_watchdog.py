# teloude/tests/test_hang_watchdog.py
"""Proofs that the hang watchdogs really terminate a blocked run.

These run real pytest sessions on a deliberately blocked test file and check the
outcome: terminated promptly, the exact node id in the report, every thread stack
dumped, non-zero exit status, no lingering watchdog process. They are the reason a
future edit cannot quietly break the safety net - the first version of the
supervisor died with a SyntaxError and guarded nothing, and a report opened
without O_APPEND once overwrote a healthy supervisor's own line, which is exactly
what `test_a_deadlocked_test_is_terminated_with_a_report` now pins down.

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

BLOCKED_SOURCE = """import time


def test_a_block_the_python_watchdog_cannot_see():
    time.sleep(600)         # stands in for a block inside native code
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
    actually uses - not just the plugin loaded by hand with `-p`.
    """
    probe = TESTS_DIR / "test_zz_watchdog_probe.py"
    probe.write_text(source, encoding="utf-8")
    report = tmp_path / "watchdog.log"
    env = dict(os.environ)
    env.update({
        "TELOUDE_WATCHDOG_FILE": str(report),
        "PYTHONPATH": str(TESTS_DIR),
        "PYTHONUNBUFFERED": "1",
    })
    env.update({name: str(value) for name, value in env_overrides.items()})
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
             str(probe)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
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


def test_the_embedded_supervisor_is_valid_python():
    """The supervisor is a program inside this file: it must actually compile."""
    plugin = _plugin()
    ast.parse(plugin._SUPERVISOR_SOURCE)          # SyntaxError = no protection
    assert "kill_pytest()" in plugin._SUPERVISOR_SOURCE
    assert "sys.exit(1)" in plugin._SUPERVISOR_SOURCE
    assert "taskkill" in plugin._SUPERVISOR_SOURCE, "Windows needs a kill it can run"


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


def test_the_configured_limits_are_short_and_the_exit_status_is_non_zero():
    """A watchdog that waits for minutes, or exits zero, is not a watchdog."""
    plugin = _plugin()
    assert 0 < plugin.TEST_LIMIT <= 60, "the per-test limit must stay short"
    assert plugin.GUARD_LIMIT >= plugin.TEST_LIMIT, "the supervisor is the backstop"
    assert plugin.GUARD_LIMIT <= plugin.TEST_LIMIT + 60
    assert plugin.WATCHDOG_EXIT_CODE != 0
    assert plugin.WATCHDOG_EXIT_CODE not in range(0, 6), "not a pytest exit status"


def test_a_deadlocked_test_is_terminated_with_a_report(tmp_path):
    result, report, elapsed, _path = _run_pytest_on(
        tmp_path, DEADLOCK_SOURCE, TELOUDE_TEST_WATCHDOG="2", TELOUDE_TEST_GUARD="6"
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


def test_the_supervisor_terminates_a_run_its_watchdog_cannot_see(tmp_path):
    """With the in-process watchdog off, the separate process must still kill it."""
    result, report, elapsed, path = _run_pytest_on(
        tmp_path, BLOCKED_SOURCE, TELOUDE_TEST_WATCHDOG="0", TELOUDE_TEST_GUARD="3"
    )

    assert result.returncode != 0, "the supervisor must fail the run"
    assert elapsed < 30, f"the supervisor took {elapsed:.1f}s to act"
    assert "supervisor confirmed alive (pid" in report, "the supervisor must prove it lives"
    assert "NO PROGRESS" in report
    assert "test_zz_watchdog_probe.py::test_a_block_the_python_watchdog_cannot_see" in report
    # The confirmation is written a moment after the process it killed is gone.
    report = _wait_for_text(path, "pytest killed")
    assert "pytest killed" in report
    # Even with the in-process watchdog off, the killed run must dump the stacks
    # of the blocked test: faulthandler gets SIGABRT before the supervisor
    # escalates to SIGKILL, and the dump goes to the report the job prints.
    assert "in test_a_block_the_python_watchdog_cannot_see" in report, (
        f"the victim's stacks were not dumped:\n{report}"
    )


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
