# teloude/tests/test_pause_resume.py
"""Bug 3 regression tests: pause/resume must be visible, honest and repeatable.

The Windows acceptance test found that Pause/Resume gave no feedback at all:
both buttons were always enabled, a click while the worker was not at a safe
point silently did nothing, and the Restore page had no Pause/Resume buttons.

These tests pin the contract that fixes it:

* the control only reports a state the worker confirmed - PAUSING is a request,
  PAUSED means the worker really parked, RESUMING means it is starting again,
* a command that cannot change anything is ignored (and not offered in the UI),
  so a repeated Pause/Resume cannot leave the UI lying about the transfer,
* every state reaches the UI as a `transfer_state` event, and both the Backup
  and the Restore page render that state instead of guessing,
* a run ends with exactly one outcome (completed / completed with errors /
  failed / cancelled).
"""
import os
import threading
import time

import pytest

from teloude.application.services import (
    BackupService,
    EventBus,
    RestoreService,
    ServiceError,
)
from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.control import ControlState, EngineCancelled, EngineControl, RunOutcome
from teloude.core.restore import RestoreManager
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    SettingsRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway
from teloude.infrastructure.telegram.files import UploadCancelled
from teloude.ui.views.run_state import STARTING, can_start, controls_for, state_label


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _parked_run(control: EngineControl, results: list) -> threading.Thread:
    """Runs wait_if_paused() in the background, recording what it did."""
    def worker():
        try:
            control.wait_if_paused()
            results.append("resumed")
        except EngineCancelled:
            results.append("cancelled")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class TestEngineControlStates:
    """The state machine itself: reported states are the worker's, not the click's."""

    @pytest.fixture()
    def states(self):
        return []

    @pytest.fixture()
    def control(self, states):
        # a short sleeper keeps the park loop responsive without slowing tests
        return EngineControl(
            sleeper=lambda seconds: time.sleep(min(seconds, 0.005)),
            on_state_change=states.append,
        )

    def test_pause_becomes_real_only_when_the_worker_parks(self, control, states):
        control.pause()
        # request accepted, but nothing has parked yet: "Pausing..."
        assert control.state is ControlState.PAUSING
        assert states == [ControlState.PAUSING]

        results = []
        thread = _parked_run(control, results)
        try:
            assert _wait_for(lambda: control.state is ControlState.PAUSED)
            assert results == [], "the worker must still be parked while paused"
            assert states == [ControlState.PAUSING, ControlState.PAUSED]
        finally:
            control.resume()
            thread.join(timeout=5)
        assert results == ["resumed"]
        assert states == [
            ControlState.PAUSING, ControlState.PAUSED, ControlState.RESUMING,
        ]
        # the transfer only counts as running again once it demonstrably moves
        assert control.should_pause() is False
        assert states[-1] is ControlState.RUNNING

    def test_repeated_commands_do_not_add_transitions(self, control, states):
        control.pause()
        control.pause()  # a second Pause while "Pausing..." must do nothing
        assert states == [ControlState.PAUSING]

        control.resume()  # undoing the pending pause is allowed
        control.resume()  # a second Resume is not
        assert states == [ControlState.PAUSING, ControlState.RESUMING]
        assert control.should_pause() is False
        assert states[-1] is ControlState.RUNNING
        control.resume()  # and Resume while running is a no-op
        assert states[-1] is ControlState.RUNNING

    def test_pause_while_already_paused_does_nothing(self, control, states):
        control.pause()
        results = []
        thread = _parked_run(control, results)
        try:
            assert _wait_for(lambda: control.state is ControlState.PAUSED)
            before = list(states)
            control.pause()
            control.pause()
            time.sleep(0.05)
            assert control.state is ControlState.PAUSED
            assert states == before, "a command that cannot change anything lied"
        finally:
            control.resume()
            thread.join(timeout=5)

    def test_resume_before_the_worker_parks_keeps_it_moving(self, control, states):
        control.pause()
        control.resume()  # the user changes their mind during "Pausing..."
        results = []
        control.wait_if_paused()  # nothing to wait for: it never parked
        assert control.should_pause() is False
        assert results == []
        assert states == [
            ControlState.PAUSING, ControlState.RESUMING, ControlState.RUNNING,
        ]

    def test_cancel_from_a_parked_worker_never_resumes(self, control, states):
        control.pause()
        results = []
        thread = _parked_run(control, results)
        assert _wait_for(lambda: control.state is ControlState.PAUSED)
        control.cancel()
        thread.join(timeout=5)
        assert results == ["cancelled"]
        assert control.state is ControlState.CANCELLING
        assert ControlState.RESUMING not in states
        with pytest.raises(EngineCancelled):
            control.check_cancelled()

    def test_pause_and_resume_after_cancel_are_ignored(self, control, states):
        control.cancel()
        with pytest.raises(EngineCancelled):
            control.wait_if_paused()
        control.pause()
        control.resume()
        assert states == [ControlState.CANCELLING]

    def test_a_broken_observer_never_stops_the_transfer(self):
        def explode(_state):
            raise RuntimeError("observer bug")

        control = EngineControl(on_state_change=explode)
        control.pause()  # must not raise
        assert control.state is ControlState.PAUSING
        control.resume()
        assert control.state is ControlState.RESUMING


class TestRunStateRules:
    """The mapping the Backup and Restore pages render (no Qt needed)."""

    def test_only_a_running_transfer_offers_pause(self):
        pause, resume, cancel = controls_for(ControlState.RUNNING.value)
        assert (pause, resume, cancel) == (True, False, True)

    def test_pausing_and_paused_offer_resume_but_not_pause(self):
        for state in (ControlState.PAUSING.value, ControlState.PAUSED.value):
            pause, resume, cancel = controls_for(state)
            assert (pause, resume, cancel) == (False, True, True), state

    def test_resuming_offers_only_cancel(self):
        assert controls_for(ControlState.RESUMING.value) == (False, False, True)

    def test_a_finished_or_unknown_run_offers_controls_to_nobody(self):
        for state in (
            None,
            RunOutcome.COMPLETED.value,
            RunOutcome.COMPLETED_WITH_ERRORS.value,
            RunOutcome.FAILED.value,
            RunOutcome.CANCELLED.value,
        ):
            assert controls_for(state) == (False, False, False), state
            assert can_start(state) is True, state

    def test_a_starting_run_blocks_starting_again(self):
        assert can_start(STARTING) is False
        assert controls_for(STARTING) == (False, False, False)

    def test_labels_name_the_transfer_not_the_button(self):
        assert state_label("backup", ControlState.RUNNING.value) == "Uploading"
        assert state_label("restore", ControlState.RUNNING.value) == "Restoring"
        assert state_label("backup", ControlState.PAUSING.value) == "Pausing\u2026"
        assert state_label("backup", ControlState.PAUSED.value) == "Paused"
        assert state_label("backup", ControlState.RESUMING.value) == "Resuming\u2026"
        assert state_label("backup", RunOutcome.COMPLETED.value) == "Completed"
        assert state_label("backup", RunOutcome.FAILED.value) == "Failed"
        assert state_label("backup", None) == ""


# --------------------------------------------------------------------------
# Service level: real runs over fakes, driven through the real control
# --------------------------------------------------------------------------
@pytest.fixture()
def ctx(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "states.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    registry = TransferRegistry(TransferRepository(manager))
    settings = SettingsRepository(manager)
    storage_gw = FakeStorageGateway()
    file_gw = FakeFileGateway()
    backup_manager = BackupManager(
        storages, folders, files, registry, storage_gw, file_gw,
        max_retries=1, retry_sleeper=lambda _s: None,
    )
    restore_manager = RestoreManager(files, registry, file_gw, max_retries=1)
    bus = EventBus()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"alpha")                  # one part
    (src / "big.bin").write_bytes(bytes(range(256)) * 400)  # several parts
    yield {
        "manager": manager, "bus": bus, "files": files, "storages": storages,
        "backup_svc": BackupService(backup_manager, files, bus),
        "restore_svc": RestoreService(restore_manager, files, storages, bus),
        "storage_gw": storage_gw, "file_gw": file_gw,
        "src": src, "tmp": tmp_path, "settings": settings,
    }
    close_db_connection(manager)


def _pause_once_during_upload(ctx, service) -> threading.Event:
    """Pauses the run from inside the worker after the first part lands."""
    gateway = ctx["file_gw"]
    real_upload = gateway.upload
    fired = threading.Event()

    def pausing_upload(local_path, progress=None, **kwargs):
        def on_progress(done):
            if progress is not None:
                progress(done)
            if not fired.is_set():
                fired.set()
                service.pause()  # the user clicks Pause while a part lands

        return real_upload(local_path, progress=on_progress, **kwargs)

    gateway.upload = pausing_upload
    return fired


def _pause_once_during_download(ctx, service) -> threading.Event:
    """Pauses the run from inside the worker after the first file is written."""
    gateway = ctx["file_gw"]
    real_download = gateway.download
    fired = threading.Event()

    def pausing_download(doc, dest_path, offset=0, progress=None, **kwargs):
        def on_progress(done):
            if progress is not None:
                progress(done)
            if not fired.is_set():
                fired.set()
                service.pause()

        return real_download(doc, dest_path, offset=offset, progress=on_progress, **kwargs)

    gateway.download = pausing_download
    return fired


def _resume_when_parked(service, control_states, timeout: float = 15.0):
    """Resumes the run from a watcher thread once the pause is real."""
    seen = {"paused": False}

    def watch():
        if _wait_for(lambda: control_states() is ControlState.PAUSED, timeout):
            seen["paused"] = True
            service.resume()

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return seen, thread


class TestBackupRunStates:
    def _states(self, ctx):
        events = []
        ctx["bus"].subscribe(
            "transfer_state", lambda p: events.append((p["kind"], p["state"]))
        )
        return events

    def test_pause_and_resume_are_broadcast_in_order(self, ctx):
        events = self._states(ctx)
        sid = ctx["storages"].create("States")
        info = ctx["storage_gw"].create_storage("States")
        ctx["storages"].set_telegram(sid, info.chat_id, True)

        _pause_once_during_upload(ctx, ctx["backup_svc"])
        seen, watcher = _resume_when_parked(ctx["backup_svc"], lambda: ctx["backup_svc"].state)
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())

        ctx["backup_svc"].start(sid, ctx["src"])
        assert done.wait(20), "the backup never finished"
        watcher.join(timeout=5)
        assert seen["paused"], "the run was never really parked"
        assert events == [
            ("backup", ControlState.RUNNING.value),
            ("backup", ControlState.PAUSING.value),
            ("backup", ControlState.PAUSED.value),
            ("backup", ControlState.RESUMING.value),
            ("backup", ControlState.RUNNING.value),
            ("backup", RunOutcome.COMPLETED.value),
        ]
        # nothing is running any more: the UI has no state to show
        assert ctx["backup_svc"].state is None
        assert ctx["backup_svc"].is_running is False

    def test_repeating_pause_while_parked_adds_no_transitions(self, ctx):
        events = self._states(ctx)
        sid = ctx["storages"].create("Repeat")
        info = ctx["storage_gw"].create_storage("Repeat")
        ctx["storages"].set_telegram(sid, info.chat_id, True)
        _pause_once_during_upload(ctx, ctx["backup_svc"])
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())

        ctx["backup_svc"].start(sid, ctx["src"])
        try:
            assert _wait_for(lambda: ctx["backup_svc"].state is ControlState.PAUSED)
            before = list(events)
            ctx["backup_svc"].pause()  # the user clicks Pause again, twice
            ctx["backup_svc"].pause()
            time.sleep(0.05)
            assert events == before, "a repeated Pause reported a change that did not happen"
            assert ctx["backup_svc"].state is ControlState.PAUSED
        finally:
            ctx["backup_svc"].resume()
        assert done.wait(20), "the backup never finished"

    def test_a_cancelled_run_ends_as_cancelled(self, ctx):
        events = self._states(ctx)
        sid = ctx["storages"].create("Stop")
        info = ctx["storage_gw"].create_storage("Stop")
        ctx["storages"].set_telegram(sid, info.chat_id, True)
        def always_cancelled(*_args, **_kwargs):
            raise UploadCancelled("cancelled")

        ctx["file_gw"].upload = always_cancelled
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())
        ctx["backup_svc"].start(sid, ctx["src"])
        ctx["backup_svc"].cancel()
        assert done.wait(20), "the cancelled backup never reported back"
        assert events[-1] == ("backup", RunOutcome.CANCELLED.value)

    def test_a_broken_run_ends_as_failed(self, ctx):
        events = self._states(ctx)
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())
        # unknown storage: the worker fails while planning
        ctx["backup_svc"].start(424242, ctx["src"])
        assert done.wait(20)
        assert events[0] == ("backup", ControlState.RUNNING.value)
        assert events[-1] == ("backup", RunOutcome.FAILED.value)
        assert ctx["backup_svc"].state is None

    def test_a_run_with_failed_files_says_so(self, ctx):
        events = self._states(ctx)
        sid = ctx["storages"].create("Partly")
        info = ctx["storage_gw"].create_storage("Partly")
        ctx["storages"].set_telegram(sid, info.chat_id, True)

        def always_fails(*_args, **_kwargs):
            raise OSError("net down")

        ctx["file_gw"].upload = always_fails
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())
        ctx["backup_svc"].start(sid, ctx["src"])
        assert done.wait(20)
        assert events[-1] == ("backup", RunOutcome.COMPLETED_WITH_ERRORS.value)

    def test_idle_services_report_no_state(self, ctx):
        assert ctx["backup_svc"].state is None
        assert ctx["restore_svc"].state is None
        with pytest.raises(ServiceError):
            ctx["restore_svc"].pause()


class TestRestoreRunStates:
    def _backed_up(self, ctx):
        sid = ctx["storages"].create("RestoreStates")
        info = ctx["storage_gw"].create_storage("RestoreStates")
        ctx["storages"].set_telegram(sid, info.chat_id, True)
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda _p: done.set())
        ctx["backup_svc"].start(sid, ctx["src"])
        assert done.wait(20)
        return sid

    def test_pause_and_resume_are_broadcast_for_restore_too(self, ctx):
        sid = self._backed_up(ctx)
        events = []
        ctx["bus"].subscribe(
            "transfer_state", lambda p: events.append((p["kind"], p["state"]))
        )
        _pause_once_during_download(ctx, ctx["restore_svc"])
        seen, watcher = _resume_when_parked(
            ctx["restore_svc"], lambda: ctx["restore_svc"].state
        )
        done = threading.Event()
        ctx["bus"].subscribe("restore_done", lambda _p: done.set())

        ctx["restore_svc"].start_storage(sid, ctx["tmp"] / "out")
        assert done.wait(20), "the restore never finished"
        watcher.join(timeout=5)
        assert seen["paused"]
        assert events == [
            ("restore", ControlState.RUNNING.value),
            ("restore", ControlState.PAUSING.value),
            ("restore", ControlState.PAUSED.value),
            ("restore", ControlState.RESUMING.value),
            ("restore", ControlState.RUNNING.value),
            ("restore", RunOutcome.COMPLETED.value),
        ]
        assert ctx["restore_svc"].state is None

    def test_a_cancelled_restore_ends_as_cancelled(self, ctx):
        sid = self._backed_up(ctx)
        events = []
        ctx["bus"].subscribe(
            "transfer_state", lambda p: events.append((p["kind"], p["state"]))
        )
        done = threading.Event()
        ctx["bus"].subscribe("restore_done", lambda _p: done.set())
        ctx["restore_svc"].start_storage(sid, ctx["tmp"] / "out")
        ctx["restore_svc"].cancel()
        assert done.wait(20)
        assert events[-1] == ("restore", RunOutcome.CANCELLED.value)


# --------------------------------------------------------------------------
# View level: the Backup and Restore pages must render the reported state
# --------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def notices(monkeypatch):
    recorded = []
    for module in (
        "teloude.ui.views.backup_view",
        "teloude.ui.views.restore_view",
    ):
        monkeypatch.setattr(
            f"{module}.show_error",
            lambda *a, _m=module, **k: recorded.append(("error", _m, a[1:])),
            raising=False,
        )
        monkeypatch.setattr(
            f"{module}.show_info",
            lambda *a, _m=module, **k: recorded.append(("info", _m, a[1:])),
            raising=False,
        )
    return recorded


@pytest.fixture()
def window(qt_app, tmp_path, notices):
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "pause.db"))
    ctx = build_offline(config)
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    win = MainWindow(ctx)
    win.show()
    qt_app.processEvents()
    yield win
    try:
        win.close()
    finally:
        ctx.shutdown()


def _pump(qt_app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)


def _pump_until(qt_app, predicate, timeout: float = 10.0) -> bool:
    """Pumps the UI event queue until `predicate()` is true or the deadline passes.

    The bounds are the point: a state that never reaches the view must fail this
    test in seconds - with the reason recorded by the caller - instead of
    waiting forever for a signal that is not coming.
    """
    deadline = time.monotonic() + timeout
    while True:
        qt_app.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            qt_app.processEvents()
            return bool(predicate())
        time.sleep(0.01)


def _report(qt_app, window, kind: str, state) -> None:
    """Delivers a real transfer_state event the way a worker thread would."""
    value = state.value if hasattr(state, "value") else state
    window._ctx.bus.emit("transfer_state", {"kind": kind, "state": value})
    _pump(qt_app, 0.05)


class TestBackupViewControls:
    def test_nothing_is_offered_before_a_run(self, window, qt_app):
        view = window.backup
        _pump(qt_app, 0.05)
        assert view.pause_button.isEnabled() is False
        assert view.resume_button.isEnabled() is False
        assert view.cancel_button.isEnabled() is False
        assert view.start_button.isEnabled() is True
        assert view.status_label.text() == "Idle."

    def test_controls_follow_each_reported_state(self, window, qt_app):
        view = window.backup
        _report(qt_app, window, "backup", ControlState.RUNNING)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled(), view.start_button.isEnabled()) == (
            True, False, True, False)
        assert view.status_label.text() == "Uploading"

        _report(qt_app, window, "backup", ControlState.PAUSING)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled()) == (False, True, True)
        assert view.status_label.text() == "Pausing\u2026"

        _report(qt_app, window, "backup", ControlState.PAUSED)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled()) == (False, True, True)
        assert view.status_label.text() == "Paused"

        _report(qt_app, window, "backup", ControlState.RESUMING)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled()) == (False, False, True)
        assert view.status_label.text() == "Resuming\u2026"

        _report(qt_app, window, "backup", RunOutcome.COMPLETED)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled(), view.start_button.isEnabled()) == (
            False, False, False, True)
        assert view.status_label.text().startswith("Completed")

    def test_a_failed_run_is_shown_as_failed(self, window, qt_app):
        view = window.backup
        _report(qt_app, window, "backup", RunOutcome.FAILED)
        assert view.status_label.text().startswith("Failed")
        assert view.pause_button.isEnabled() is False

    def test_restore_events_never_move_the_backup_buttons(self, window, qt_app):
        view = window.backup
        _report(qt_app, window, "restore", ControlState.RUNNING)
        assert view.pause_button.isEnabled() is False
        assert view.status_label.text() == "Idle."


class TestRestoreViewControls:
    def test_restore_offers_pause_and_resume(self, window, qt_app):
        view = window.restore
        _pump(qt_app, 0.05)
        assert view.pause_button.isEnabled() is False
        assert view.resume_button.isEnabled() is False
        assert view.cancel_button.isEnabled() is False

    def test_controls_follow_each_reported_state(self, window, qt_app):
        view = window.restore
        _report(qt_app, window, "restore", ControlState.RUNNING)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled(), view.start_button.isEnabled(),
                view.storage_button.isEnabled()) == (True, False, True, False, False)
        assert view.status_label.text() == "Restoring"

        _report(qt_app, window, "restore", ControlState.PAUSED)
        assert (view.pause_button.isEnabled(), view.resume_button.isEnabled(),
                view.cancel_button.isEnabled()) == (False, True, True)
        assert view.status_label.text() == "Paused"

        _report(qt_app, window, "restore", RunOutcome.COMPLETED)
        assert view.start_button.isEnabled() is True
        assert view.storage_button.isEnabled() is True
        assert view.pause_button.isEnabled() is False

    def test_backup_events_never_move_the_restore_buttons(self, window, qt_app):
        view = window.restore
        _report(qt_app, window, "backup", ControlState.RUNNING)
        assert view.pause_button.isEnabled() is False
        assert view.status_label.text() == "Idle."


# ---------------------------------------------------------------------------
# A live run driven from the page
# ---------------------------------------------------------------------------
# The transfer is deliberately slowed down and held between two parts, so the
# sequence "the transfer is moving -> the user clicks -> the engine parks" is
# deterministic on any machine instead of racing the end of the file. When a
# transfer is fast enough, a Pause request that arrives after the last part of
# the last file can never park the worker, and a test waiting for PAUSED would
# then wait for a state that cannot happen any more.
_LIVE_BYTES = bytes(range(256)) * 800     # 200 KiB: worth many parts
_LIVE_PARTS = 64                          # ~3 KiB per part
_MS_PER_PART = 0.01                       # a network moves in steps, not at once
_LIVE_CLICK_WINDOW = 30.0                 # how long the transfer may be held still


def _start_a_held_live_backup(window, tmp_path):
    """Starts a real backup and holds it mid-transfer until the gate is set.

    Returns the view, an event that proves the transfer moved, the gate the test
    releases after clicking, and the progress reports the gateway produced (kept
    as evidence for a transition that fails to arrive). Nothing about the
    engine's pause/resume behaviour is faked: the run really stops at a safe
    point and really continues afterwards.
    """
    ctx = window._ctx
    storage = ctx.services.storages.create_storage("Interactive")
    source = tmp_path / "bulk"
    source.mkdir()
    (source / "big.bin").write_bytes(_LIVE_BYTES)
    (source / "second.bin").write_bytes(b"more")

    gateway = ctx.backup_manager._gateway
    real_upload = gateway.upload
    # Small parts keep the transfer in flight long enough to click a button.
    gateway.suggest_part_size = lambda size: max(1024, len(_LIVE_BYTES) // _LIVE_PARTS)
    moved = threading.Event()
    gate = threading.Event()
    progress_reports: list = []

    def held_upload(local_path, progress=None, **kwargs):
        def on_progress(done):
            if progress is not None:
                progress(done)          # the real run still records progress
            progress_reports.append(done)
            moved.set()
            # Bounded, so a test that never clicks cannot stall the worker.
            gate.wait(timeout=_LIVE_CLICK_WINDOW)
            time.sleep(_MS_PER_PART)

        return real_upload(local_path, progress=on_progress, **kwargs)

    gateway.upload = held_upload
    view = window.backup
    view.storage_combo.setCurrentIndex(view.storage_combo.findData(storage.id))
    view.folder_edit.setText(str(source))
    view._on_start()
    return view, moved, gate, progress_reports


def _state_evidence(view, parts) -> str:
    """Everything needed to understand a state that never arrived."""
    ctx = view._ctx
    state = ctx.services.backup.state
    return (
        f"state={getattr(state, 'value', state)!r}, "
        f"running={ctx.services.backup.is_running!r}, "
        f"parts_uploaded={len(parts)}, "
        f"status={view.status_label.text()!r}, "
        f"pause={view.pause_button.isEnabled()!r}, "
        f"resume={view.resume_button.isEnabled()!r}"
    )


class TestLivePauseFromTheUi:
    """A real run, paused and resumed through the actual buttons.

    This is the acceptance criterion of Bug 3 end to end: a transfer is really
    moving, the user clicks Pause, the engine really parks, the buttons describe
    that state, the user clicks Resume and the same run really finishes.
    """

    def test_the_pause_and_resume_buttons_drive_a_live_run(self, window, qt_app, tmp_path):
        view, moved, gate, parts = _start_a_held_live_backup(window, tmp_path)
        ctx = window._ctx
        try:
            # The user can only pause a transfer that is actually moving.
            assert _wait_for(moved.is_set, timeout=10.0), "the upload never moved"
            assert ctx.services.backup.is_running is True, (
                "the run ended before Pause could be clicked: "
                + _state_evidence(view, parts)
            )
            # A disabled button would swallow the click, so the page must first
            # have shown the running state it was told about.
            assert _pump_until(qt_app, view.pause_button.isEnabled), (
                "Pause never became available: " + _state_evidence(view, parts)
            )

            view.pause_button.click()   # the real button, the real command
            gate.set()                  # ... and the transfer is free to move again
            assert _wait_for(
                lambda: ctx.services.backup.state is ControlState.PAUSED, timeout=15.0
            ), "the run never parked after Pause: " + _state_evidence(view, parts)

            assert _pump_until(
                qt_app, lambda: view.status_label.text().startswith("Paused")
            ), "the paused state never reached the page: " + _state_evidence(view, parts)
            assert view.pause_button.isEnabled() is False
            assert view.resume_button.isEnabled() is True
            assert view.cancel_button.isEnabled() is True
            assert view.start_button.isEnabled() is False

            view.resume_button.click()  # the real button, the real command
            assert _wait_for(
                lambda: not ctx.services.backup.is_running, timeout=15.0
            ), "the run never finished after Resume: " + _state_evidence(view, parts)
            assert _pump_until(
                qt_app, lambda: view.status_label.text().startswith("Completed")
            ), "the finished state never reached the page: " + _state_evidence(view, parts)
        finally:
            if ctx.services.backup.is_running:
                ctx.services.backup.cancel()
                _wait_for(lambda: not ctx.services.backup.is_running, timeout=10.0)

        assert view.status_label.text().startswith("Completed"), view.status_label.text()
        assert "Uploaded 2" in view.status_label.text(), view.status_label.text()
        assert view.start_button.isEnabled() is True
        assert view.resume_button.isEnabled() is False

    def test_the_cancel_button_stops_a_live_run(self, window, qt_app, tmp_path):
        """Cancel is the third command on the bar: it must really stop the run."""
        view, moved, gate, parts = _start_a_held_live_backup(window, tmp_path)
        ctx = window._ctx
        try:
            assert _wait_for(moved.is_set, timeout=10.0), "the upload never moved"
            assert _pump_until(qt_app, view.cancel_button.isEnabled), (
                "Cancel never became available: " + _state_evidence(view, parts)
            )

            view.cancel_button.click()  # the real button, the real command
            gate.set()
            assert _wait_for(
                lambda: not ctx.services.backup.is_running, timeout=15.0
            ), "the run kept going after Cancel: " + _state_evidence(view, parts)
            assert _pump_until(
                qt_app, lambda: view.status_label.text().startswith("Cancelled")
            ), "the cancelled state never reached the page: " + _state_evidence(view, parts)
        finally:
            if ctx.services.backup.is_running:
                ctx.services.backup.cancel()
                _wait_for(lambda: not ctx.services.backup.is_running, timeout=10.0)

        # Cancelled means stopped: every command that moves a run is gone again.
        assert view.pause_button.isEnabled() is False
        assert view.resume_button.isEnabled() is False
        assert view.cancel_button.isEnabled() is False
        assert view.start_button.isEnabled() is True



class TestShutdownReportsNothingStale:
    """What the pages see while the window is going away.

    A run that already finished is not "cancelled by shutdown", and a page that
    is already gone must not receive another update. Both used to happen: the
    teardown cancelled every service unconditionally, so closing the window
    broadcast a transfer_state for a run that was over, and the queued event was
    delivered to whichever page ran next - which is exactly the kind of late
    delivery that makes a teardown behave differently from machine to machine.
    """

    def test_a_finished_run_is_not_cancelled_again_at_shutdown(self, window, qt_app,
                                                               tmp_path):
        view, _moved, gate, parts = _start_a_held_live_backup(window, tmp_path)
        ctx = window._ctx
        gate.set()
        assert _wait_for(lambda: not ctx.services.backup.is_running, timeout=15.0), (
            "the run never finished: " + _state_evidence(view, parts)
        )
        _pump_until(qt_app, lambda: view.status_label.text().startswith("Completed"))

        events: list = []
        ctx.bus.subscribe("transfer_state", events.append)
        ctx.shutdown()  # the real teardown, exactly as the window does it

        assert events == [], f"shutdown broadcast a stale state: {events}"

    def test_shutdown_stops_delivering_updates_to_the_page(self, window, qt_app):
        ctx = window._ctx
        view = window.backup
        before = view.status_label.text()

        ctx.shutdown()
        # A service that reports after the window is gone must reach nobody: the
        # event is published on the bus, but the bridge has unhooked itself.
        ctx.bus.emit("transfer_state", {"kind": "backup", "state": "paused"})
        _pump(qt_app, 0.1)

        assert view.status_label.text() == before
        assert view.pause_button.isEnabled() is False
        assert view.resume_button.isEnabled() is False
