# teloude/tests/test_first_run_journey.py
"""The v1 walkthrough, start to finish, in one place.

Spec §47 lists the journeys a user must be able to complete. The other suites
test the pieces; this one runs the whole thing the way a new user would, so a
regression that only shows up in the combination fails here.
"""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.config import AppConfig  # noqa: E402
from teloude.infrastructure.telegram.auth import AuthState  # noqa: E402
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
        "teloude.ui.views.settings_view",
        "teloude.ui.views.storages",
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


def _pump(qt_app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_whole_first_run_journey(qt_app, tmp_path, notices):
    """Sign in, create a storage, back up, inspect, restore, and stay informed."""
    # 1-2. the app starts, a session exists (the offline double mirrors the wizard)
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "journey.db"))
    ctx = build_offline(config)
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    assert ctx.services.auth.state in (AuthState.AUTHORIZED, AuthState.SIGNED_OUT)

    window = MainWindow(ctx)
    window.show()
    qt_app.processEvents()
    try:
        # 3. create a storage
        storage = ctx.services.storages.create_storage("Journey")
        qt_app.processEvents()
        assert [s.name for s in ctx.services.storages.list()] == ["Journey"]

        # 4-6. pick a folder and back it up, watching progress
        source = tmp_path / "documents"
        (source / "reports").mkdir(parents=True)
        (source / "notes.txt").write_bytes(b"hello teloude\n" * 100)
        (source / "reports" / "q1.csv").write_bytes(b"a,b,c\n1,2,3\n")
        (source / "reports" / "q2.csv").write_bytes(b"a,b,c\n4,5,6\n")
        (source / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

        progress_seen = []
        done = threading.Event()
        ctx.bus.subscribe("backup_progress", lambda p: progress_seen.append(p))
        ctx.bus.subscribe("backup_done", lambda _p: done.set())

        view = window.backup
        view.storage_combo.setCurrentIndex(view.storage_combo.findData(storage.id))
        view.folder_edit.setText(str(source))
        view._on_start()
        assert done.wait(timeout=20), "the backup never finished"
        _pump(qt_app, 0.5)

        assert progress_seen, "the user must see progress"
        assert "Uploaded 4" in view.status_label.text(), view.status_label.text()
        indexed = {f.relative_path for f in ctx.repos.files.list_by_storage(storage.id)}
        assert indexed == {"notes.txt", "reports/q1.csv", "reports/q2.csv", "photo.png"}

        # 10. running again is a real no-op: nothing is re-uploaded
        again = threading.Event()
        done_payload = {}
        ctx.bus.subscribe("backup_done",
                          lambda p: (done_payload.update(p), again.set()))
        ctx.services.backup.start(storage.id, source)  # default policy: skip
        assert again.wait(timeout=20)
        _pump(qt_app, 0.3)
        assert done_payload.get("uploaded") == 0, done_payload
        assert done_payload.get("unchanged") == 4, done_payload
        assert "Uploaded 0" in view.status_label.text(), view.status_label.text()
        assert "4 already up to date" in view.status_label.text(), view.status_label.text()

        # 10b. editing one file uploads only that file and drops the old copy
        (source / "notes.txt").write_text("first run notes, second version", encoding="utf-8")
        third = threading.Event()
        third_payload = {}
        ctx.bus.subscribe("backup_done",
                          lambda p: (third_payload.update(p), third.set()))
        ctx.services.backup.start(storage.id, source)
        assert third.wait(timeout=20)
        assert third_payload.get("uploaded") == 1, third_payload
        assert third_payload.get("unchanged") == 3, third_payload

        # 11. search finds it
        entries = ctx.services.search.search("q1")
        assert [e.relative_path for e in entries] == ["reports/q1.csv"]

        # 12. restore one file and a whole folder into a fresh tree
        destination = tmp_path / "restored"
        q1 = [f for f in ctx.repos.files.list_by_storage(storage.id)
              if f.relative_path.endswith("q1.csv")][0]
        ctx.services.restore.start_files([q1.id], destination)
        assert _wait_for(lambda: not ctx.services.restore.is_running)
        assert (destination / "reports" / "q1.csv").read_bytes() == \
            (source / "reports" / "q1.csv").read_bytes()

        whole = tmp_path / "restored-all"
        ctx.services.restore.start_storage(storage.id, whole)
        assert _wait_for(lambda: not ctx.services.restore.is_running)
        for relative in ("notes.txt", "reports/q1.csv", "reports/q2.csv", "photo.png"):
            restored = whole / relative
            assert restored.exists(), relative
            # byte-identical content, verified against the local original
            assert restored.read_bytes() == (source / relative).read_bytes(), relative

        # 6. a second storage can be added and used independently
        second = ctx.services.storages.create_storage("Second")
        assert len(ctx.services.storages.list()) == 2
        other_source = tmp_path / "other"
        other_source.mkdir()
        (other_source / "only.txt").write_bytes(b"second storage")
        ctx.services.backup.start(second.id, other_source)
        assert _wait_for(lambda: not ctx.services.backup.is_running)
        assert [f.relative_path for f in ctx.repos.files.list_by_storage(second.id)] == \
            ["only.txt"]

        # 15. the dashboard reflects the finished work
        window.nav.setCurrentRow(0)
        _pump(qt_app, 0.5)
        dashboard_text = " ".join(
            window.dashboard.storage_list.item(row).text()
            for row in range(window.dashboard.storage_list.count())
        )
        assert "Journey" in dashboard_text

        # and no error was ever reported to the user along the way
        errors = [notice for notice in notices if notice[0] == "error"]
        assert errors == [], errors
    finally:
        window.close()
        ctx.shutdown()


def test_restore_dialog_sees_the_new_backup_and_uses_it(qt_app, tmp_path, notices):
    """A restore started from the UI reads the same index the backup wrote."""
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "journey2.db"))
    ctx = build_offline(config)
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    window = MainWindow(ctx)
    window.show()
    try:
        source = tmp_path / "src"
        source.mkdir()
        (source / "single.txt").write_bytes(b"one file")
        storage = ctx.services.storages.create_storage("UiJourney")
        ctx.services.backup.start(storage.id, source)
        assert _wait_for(lambda: not ctx.services.backup.is_running)

        view = window.restore
        view.refresh_storages()
        index = view.storage_combo.findData(storage.id)
        assert index >= 0, "the new storage must be selectable"
        view.storage_combo.setCurrentIndex(index)
        qt_app.processEvents()
        assert view.tree.topLevelItemCount() == 1  # the "(root)" folder
        item = view.tree.topLevelItem(0)
        item.setCheckState(0, item.checkState(0).Checked)
        _pump(qt_app, 0.2)
        assert len(view._checked_ids()) == 1

        destination = tmp_path / "out"
        view.dest_edit.setText(str(destination))
        view._on_start()
        assert _wait_for(lambda: not ctx.services.restore.is_running)
        assert (destination / "single.txt").read_bytes() == b"one file"
        assert [notice for notice in notices if notice[0] == "error"] == []
    finally:
        window.close()
        ctx.shutdown()
