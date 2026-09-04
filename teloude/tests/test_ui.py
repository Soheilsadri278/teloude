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


def test_settings_speed_limit_persists(window, qt_app):
    settings = window.settings
    settings.speed_combo.setCurrentIndex(settings.speed_combo.findData(5.0))
    settings._on_apply_speed()
    _pump(qt_app, 0.3)
    assert window._ctx.services.settings.get_speed_limit_mbps() == 5.0
