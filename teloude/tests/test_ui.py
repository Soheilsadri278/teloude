# teloude/tests/test_ui.py
"""Headless UI smoke tests (offscreen platform, offline fakes, no network).

Exercises real service + view wiring: navigation, backup run to completion,
search results, and settings persistence.
"""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.config import AppConfig  # noqa: E402
from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def window(qt_app, tmp_path, monkeypatch):
    # Modal message boxes would block forever offscreen: record instead.
    notices = []
    monkeypatch.setattr("teloude.ui.views.backup_view.show_error",
                        lambda *a, **k: notices.append(("error", a[1:])))
    monkeypatch.setattr("teloude.ui.views.backup_view.show_info",
                        lambda *a, **k: notices.append(("info", a[1:])))
    monkeypatch.setattr("teloude.ui.views.restore_view.show_error",
                        lambda *a, **k: notices.append(("error", a[1:])))
    monkeypatch.setattr("teloude.ui.views.restore_view.show_info",
                        lambda *a, **k: notices.append(("info", a[1:])))
    monkeypatch.setattr("teloude.ui.views.settings_view.show_error",
                        lambda *a, **k: notices.append(("error", a[1:])))
    monkeypatch.setattr("teloude.ui.views.settings_view.show_info",
                        lambda *a, **k: notices.append(("info", a[1:])))
    monkeypatch.setattr("teloude.ui.views.storages.show_error",
                        lambda *a, **k: notices.append(("error", a[1:])))
    monkeypatch.setattr("teloude.ui.views.storages.show_info",
                        lambda *a, **k: notices.append(("info", a[1:])))
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "test.db"))
    ctx = build_offline(config)
    ctx.notices = notices
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    win = MainWindow(ctx)
    win.show()
    qt_app.processEvents()
    yield win
    try:
        ctx.services.backup.cancel()
    except Exception:
        pass
    try:
        ctx.services.restore.cancel()
    except Exception:
        pass
    win.close()
    ctx.shutdown()


def _pump(qt_app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)


def test_navigation_pages(window, qt_app):
    assert window.nav.count() == 7
    for row in range(window.nav.count()):
        window.nav.setCurrentRow(row)
        qt_app.processEvents()
        assert window.stack.currentIndex() == row


def test_backup_flow_to_completion(window, qt_app, tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "hello.txt").write_bytes(b"hello teloude")
    record = window._ctx.services.storages.create_storage("Smoke")
    qt_app.processEvents()
    view = window.backup
    index = view.storage_combo.findData(record.id)
    assert index >= 0
    view.storage_combo.setCurrentIndex(index)
    view.folder_edit.setText(str(src))
    done = threading.Event()
    window._ctx.bus.subscribe("backup_done", lambda _p: done.set())
    view._on_start()
    assert done.wait(timeout=15), "backup run did not finish"
    _pump(qt_app, 0.5)
    assert "Uploaded 1" in view.status_label.text()
    assert view.progress.value() > 0
    rows = window._ctx.repos.files.list_by_storage(record.id)
    assert len(rows) == 1 and rows[0].is_backed_up


def test_content_verification_checkbox_reaches_the_service(window, qt_app, tmp_path):
    src = tmp_path / "checked"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x")
    record = window._ctx.services.storages.create_storage("Checked")
    qt_app.processEvents()
    calls = []
    window._ctx.services.backup.start = lambda *a, **kw: calls.append((a, kw))
    view = window.backup
    view.storage_combo.setCurrentIndex(view.storage_combo.findData(record.id))
    view.folder_edit.setText(str(src))

    view._on_start()
    assert calls[-1][1]["verify_content"] is False  # default: fast tier

    view.verify_check.setChecked(True)
    view.start_button.setEnabled(True)
    view._on_start()
    assert calls[-1][1]["verify_content"] is True


def test_search_finds_backed_up_file(window, qt_app, tmp_path):
    src = tmp_path / "docs2"
    src.mkdir()
    (src / "report_final.txt").write_bytes(b"data")
    record = window._ctx.services.storages.create_storage("Smoke2")
    done = threading.Event()
    window._ctx.bus.subscribe("backup_done", lambda _p: done.set())
    window._ctx.services.backup.start(record.id, src)
    assert done.wait(timeout=15)
    window.search.query_edit.setText("report_final")
    window.search._on_search()
    deadline = time.monotonic() + 10
    while window.search.results.rowCount() == 0 and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)
    assert window.search.results.rowCount() == 1
    assert window.search.results.item(0, 0).text() == "report_final.txt"


def test_restore_tree_folder_selection(window, qt_app, tmp_path):
    from PySide6 import QtCore

    src = tmp_path / "nested"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "photo.jpg").write_bytes(b"jpeg-bytes")
    (src / "top.txt").write_bytes(b"top")
    record = window._ctx.services.storages.create_storage("Tree")
    done = threading.Event()
    window._ctx.bus.subscribe("backup_done", lambda _p: done.set())
    window._ctx.services.backup.start(record.id, src)
    assert done.wait(timeout=15)

    view = window.restore
    view.refresh_storages()
    qt_app.processEvents()
    assert view.storage_combo.findData(record.id) >= 0
    view.storage_combo.setCurrentIndex(view.storage_combo.findData(record.id))
    qt_app.processEvents()
    assert view.tree.topLevelItemCount() == 2  # "sub" folder + "(root)"

    folder_item = None
    root = view.tree.invisibleRootItem()
    for row in range(root.childCount()):
        candidate = root.child(row)
        if candidate.text(0) == "sub":
            folder_item = candidate
    assert folder_item is not None
    # file rows appear on expansion (the tree is built lazily for big storages)
    folder_item.setExpanded(True)
    qt_app.processEvents()
    assert folder_item.childCount() == 1
    assert folder_item.child(0).text(0) == "photo.jpg"

    folder_item.setCheckState(0, QtCore.Qt.CheckState.Checked)
    qt_app.processEvents()
    assert folder_item.child(0).checkState(0) == QtCore.Qt.CheckState.Checked
    assert len(view._checked_ids()) == 1  # whole-folder selection restores its file

    view._set_all(False)
    assert view._checked_ids() == []
    assert folder_item.child(0).checkState(0) == QtCore.Qt.CheckState.Unchecked


def test_settings_speed_limit_persists(window, qt_app):
    settings = window.settings
    settings.speed_combo.setCurrentIndex(settings.speed_combo.findData(5.0))
    settings._on_apply_speed()
    _pump(qt_app, 0.3)
    assert window._ctx.services.settings.get_speed_limit_mbps() == 5.0


def _select_storage(view, storage_id) -> None:
    index = view.storage_combo.findData(storage_id)
    assert index >= 0, "storage did not reach the view"
    view.storage_combo.setCurrentIndex(index)


def test_backup_validation_messages(window, qt_app, tmp_path):
    """Missing storage / folder must be explained, not crash or start a run."""
    view = window.backup
    n_before = len(window._ctx.notices)
    view._on_start()
    assert window._ctx.notices[n_before:] == [
        ("info", ("Backup", "Create a storage first (Storages tab)."))
    ]
    assert view.start_button.isEnabled()

    record = window._ctx.services.storages.create_storage("Validation")
    qt_app.processEvents()
    _select_storage(view, record.id)

    view.folder_edit.setText("")
    n_before = len(window._ctx.notices)
    view._on_start()
    assert window._ctx.notices[n_before:] == [
        ("info", ("Backup", "Select a local folder first."))
    ]
    assert view.start_button.isEnabled()

    view.folder_edit.setText(str(tmp_path / "does-not-exist"))
    n_before = len(window._ctx.notices)
    view._on_start()
    assert window._ctx.notices[n_before:], "a bad source folder must be reported"
    kind, args = window._ctx.notices[n_before]
    assert kind == "info" and args[0] == "Backup"
    assert "does-not-exist" in args[1]
    assert view.start_button.isEnabled()  # never left disabled by a failed start

    a_file = tmp_path / "a-file.txt"
    a_file.write_bytes(b"not a folder")
    view.folder_edit.setText(str(a_file))
    n_before = len(window._ctx.notices)
    view._on_start()
    assert "not a folder" in window._ctx.notices[n_before][1][1]


def test_backup_failure_is_reported_in_the_ui(window, qt_app, tmp_path):
    """A file that cannot be uploaded must surface as a visible failure."""
    src = tmp_path / "broken"
    src.mkdir()
    (src / "file.bin").write_bytes(b"x" * 4096)
    record = window._ctx.services.storages.create_storage("Broken")
    qt_app.processEvents()
    view = window.backup
    _select_storage(view, record.id)
    view.folder_edit.setText(str(src))

    # one unrecoverable upload error (retries disabled so the run fails fast)
    window._ctx.backup_manager._max_retries = 0
    window._ctx.backup_manager._gateway.fail_next_upload_with = OSError("network down")

    done = threading.Event()
    window._ctx.bus.subscribe("backup_done", lambda _p: done.set())
    view._on_start()
    assert done.wait(timeout=15)
    _pump(qt_app, 0.4)

    assert "failed 1" in view.status_label.text(), view.status_label.text()
    errors = [n for n in window._ctx.notices if n[0] == "error"]
    assert any("Backup finished with errors" == n[1][0] for n in errors), window._ctx.notices
    assert any("network down" in str(n[1][1]) for n in errors)
    assert view.start_button.isEnabled()
    rows = window._ctx.repos.files.list_by_storage(record.id)
    assert [r.is_backed_up for r in rows] == [False], "a failed upload must not look backed up"
    # the transfers page shows the failed row so the user can retry
    # (pages: 0 Overview, 1 Storages, 2 Backup, 3 Transfers, 4 Restore, ...)
    window.nav.setCurrentRow(3)
    assert window.stack.currentWidget() is window.transfers
    _pump(qt_app, 0.3)  # the page refreshes when it becomes visible
    statuses = [window.transfers.table.item(row, 2).text().lower()
                for row in range(window.transfers.table.rowCount())]
    errors = [window.transfers.table.item(row, 4).text()
              for row in range(window.transfers.table.rowCount())]
    assert "failed" in statuses, statuses
    assert any("network down" in text for text in errors), errors


def test_restore_error_paths_are_reported(window, qt_app, tmp_path):
    view = window.restore
    n_before = len(window._ctx.notices)
    view._on_start()  # nothing selected
    assert window._ctx.notices[n_before:] == [
        ("info", ("Restore", "Select at least one file or folder."))
    ]

    n_before = len(window._ctx.notices)
    view._on_restore_storage()  # no storage chosen
    assert window._ctx.notices[n_before:] == [
        ("info", ("Restore", "Select a storage first."))
    ]

    record = window._ctx.services.storages.create_storage("Empty")
    qt_app.processEvents()
    _select_storage(view, record.id)

    view.dest_edit.setText("")
    n_before = len(window._ctx.notices)
    view._on_restore_storage()  # storage chosen, but no destination yet
    assert window._ctx.notices[n_before:] == [
        ("info", ("Restore", "Choose a restore destination."))
    ]

    view.dest_edit.setText(str(tmp_path / "restore-out"))
    n_before = len(window._ctx.notices)
    view._on_restore_storage()  # storage exists but holds nothing
    kind, args = window._ctx.notices[n_before]
    assert kind == "error" and args[0] == "Restore failed to start"
    assert "no backed-up files" in args[1].lower()


def test_storage_duplicate_name_is_rejected_in_the_ui(window, qt_app, monkeypatch):
    """Creating a second storage with the same name fails visibly, not silently."""
    page = window.storages
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText",
        staticmethod(lambda *a, **k: ("Unique", True)),
    )
    page._on_create()
    _pump(qt_app, 0.5)
    assert len(window._ctx.services.storages.list()) == 1

    n_before = len(window._ctx.notices)
    page._on_create()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(window._ctx.notices) == n_before:
        _pump(qt_app, 0.1)
    assert len(window._ctx.services.storages.list()) == 1, "duplicate was created"
    kind, args = window._ctx.notices[n_before]
    assert kind == "error" and args[0] == "Create failed"
    assert "already exists" in args[1]
    assert page.isEnabled()  # the view is not left disabled
