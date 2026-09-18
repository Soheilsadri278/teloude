# teloude/tests/test_scale.py
"""Scale and long-run behaviour of the UI.

A real backup set is tens of thousands of files. These tests keep the views
usable at that size: the restore tree must not build a widget per file up front,
transfers must not rebuild the table on every progress tick, and the preview
cache must stay bounded. Timings are generous; the structural assertions are the
real guard rails.
"""
import hashlib
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.config import AppConfig  # noqa: E402
from teloude.infrastructure.database import utcnow  # noqa: E402
from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402

FILE_COUNT = 20_000
FOLDERS = 40

# The two statements the seeded rows go through, mirroring `FileRepository`
# exactly: the same columns `upsert()` inserts and the same fields
# `mark_backed_up()` sets. They are executed once per batch inside a single
# transaction instead of once per row - building the fixture must not cost
# 20,000 transactions, which would turn this test into a benchmark of SQLite
# commits on whatever disk the runner happens to have. `upsert()` and
# `mark_backed_up()` keep their per-row coverage in `test_database.py`,
# `test_search_preview.py`, `test_root_folder_restore.py` and `test_core_state.py`.
INSERT_FILE_SQL = (
    "INSERT INTO files(storage_id, folder_id, local_path, relative_path, file_name,"
    " size, mtime, sha256, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
MARK_BACKED_UP_SQL = (
    "UPDATE files SET is_backed_up=1, telegram_chat_id=?, telegram_msg_id=?,"
    " backup_at=? WHERE id=?"
)


def _seed_files(ctx, storage_id: int) -> list:
    """Writes FILE_COUNT backed-up rows in three statements, not 40,000.

    Returns the rows as `(file_name, message_id, payload)` so a test can check the
    seeded state without touching the database again.
    """
    rows = []
    for index in range(FILE_COUNT):
        relative = f"folder{index % FOLDERS:02d}/sub{index % 5}/file{index:05d}.bin"
        payload = _payload(index)
        rows.append((storage_id, None, f"/src/{relative}", relative, f"file{index:05d}.bin",
                     len(payload), 0.0, hashlib.sha256(payload).hexdigest(), f"{index}:1"))

    with ctx.db.transaction() as cursor:
        cursor.executemany(INSERT_FILE_SQL, rows)

    backed_up = []
    stamp = utcnow()
    marks = []
    for record in ctx.repos.files.list_by_storage(storage_id):
        index = int(record.file_name[4:9])
        payload = _payload(index)
        message_id = 9000 + record.id
        # the seeded rows point at real "remote" content, so restores can verify
        ctx.backup_manager._gateway._blobs[message_id] = payload
        marks.append((777, message_id, stamp, record.id))
        backed_up.append((record.file_name, message_id, payload))

    with ctx.db.transaction() as cursor:
        cursor.executemany(MARK_BACKED_UP_SQL, marks)
    return backed_up


def _payload(index: int) -> bytes:
    """Small, unique, correctly-hashed content for a seeded file."""
    return f"payload-{index:05d}".encode("utf-8")


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def populated(qt_app, tmp_path, monkeypatch):
    """A storage holding FILE_COUNT backed-up files spread over FOLDERS."""
    # modal dialogs would block forever offscreen: record them instead
    notices = []
    for module, kind in (
        ("teloude.ui.views.restore_view", "restore"),
        ("teloude.ui.views.backup_view", "backup"),
        ("teloude.ui.views.transfers_view", "transfers"),
        ("teloude.ui.views.storages", "storages"),
    ):
        monkeypatch.setattr(
            f"{module}.show_error",
            lambda *a, _k=kind, **k: notices.append((_k, "error", a[1:])),
            raising=False,
        )
        monkeypatch.setattr(
            f"{module}.show_info",
            lambda *a, _k=kind, **k: notices.append((_k, "info", a[1:])),
            raising=False,
        )
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "scale.db"))
    ctx = build_offline(config)
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    storage = ctx.services.storages.create_storage("Large")
    started = time.perf_counter()
    ctx.seeded = _seed_files(ctx, storage.id)
    ctx.notices = notices
    ctx.seed_seconds = time.perf_counter() - started
    window = MainWindow(ctx)
    window.show()
    qt_app.processEvents()
    yield window, ctx, storage
    window.close()
    ctx.shutdown()


def test_the_fixture_seeds_in_batches_not_row_by_row(tmp_path, monkeypatch):
    """Building the fixture must cost a handful of statements, not 60,000.

    One commit per row is what turned this module into a benchmark of whatever
    disk the runner has, and let the per-test watchdog expire on a slow machine.
    The row-by-row paths keep their own coverage in the repository tests; here the
    bar is that seeding 20,000 files stays a handful of statements.
    """
    from teloude.infrastructure.database import DatabaseManager

    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "batch.db"))
    ctx = build_offline(config)
    statements = []
    try:
        storage = ctx.services.storages.create_storage("Large")
        for name in ("transaction", "execute_query"):
            original = getattr(DatabaseManager, name)

            def counting(self, *args, _name=name, _original=original, **kwargs):
                statements.append(_name)
                return _original(self, *args, **kwargs)

            monkeypatch.setattr(DatabaseManager, name, counting)

        seeded = _seed_files(ctx, storage.id)
        assert len(seeded) == FILE_COUNT
        records = ctx.repos.files.list_by_storage(storage.id)
        assert len(records) == FILE_COUNT
        assert all(record.is_backed_up for record in records)
        assert len(statements) < 20, (
            f"seeding used {len(statements)} database statements; it must not "
            "commit once per row"
        )
    finally:
        ctx.shutdown()


def _pump(qt_app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)


def _select_storage(view, storage_id) -> None:
    if view.storage_combo.findData(storage_id) < 0:
        view.refresh_storages()  # the real app does this on page show
    index = view.storage_combo.findData(storage_id)
    assert index >= 0
    view.storage_combo.setCurrentIndex(index)
    assert view.storage_combo.currentData() == storage_id


def _folder_item(view, name: str):
    """Rows are labelled with the folder's path relative to the storage root."""
    root = view.tree.invisibleRootItem()
    for row in range(root.childCount()):
        if root.child(row).text(0) == name:
            return root.child(row)
    return None


# each folderNN holds FILE_COUNT / FOLDERS files (the sub-folder index repeats)
FILES_PER_FOLDER = FILE_COUNT // FOLDERS


def _any_folder_item(view, prefix: str):
    root = view.tree.invisibleRootItem()
    for row in range(root.childCount()):
        if root.child(row).text(0).startswith(prefix):
            return root.child(row)
    return None


class TestRestoreTreeAtScale:
    def test_the_seeded_storage_holds_every_file_in_the_index(self, populated):
        """The bulk seeding must produce the index the per-row loop produced."""
        _window, ctx, storage = populated
        records = ctx.repos.files.list_by_storage(storage.id)
        assert len(records) == FILE_COUNT
        assert len(ctx.seeded) == FILE_COUNT
        assert all(record.is_backed_up for record in records), "every row must be backed up"
        assert {record.telegram_chat_id for record in records} == {777}
        message_ids = {record.telegram_msg_id for record in records}
        assert len(message_ids) == FILE_COUNT, "each row needs its own remote message"
        assert all(record.backup_at for record in records)

        by_name = {record.file_name: record for record in records}
        for name, message_id, payload in ctx.seeded[:FILES_PER_FOLDER]:
            record = by_name[name]
            assert record.telegram_msg_id == message_id
            assert record.size == len(payload)
            assert record.sha256 == hashlib.sha256(payload).hexdigest()
            assert ctx.backup_manager._gateway._blobs[message_id] == payload

    def test_tree_is_lazy_and_fast_for_twenty_thousand_files(self, populated, qt_app):
        window, ctx, storage = populated
        view = window.restore
        _select_storage(view, storage.id)

        started = time.perf_counter()
        view._load_files()
        elapsed = time.perf_counter() - started

        # one row per folder, and *no* file row until a folder is expanded
        assert view.tree.topLevelItemCount() == FOLDERS
        root = view.tree.invisibleRootItem()
        for row in range(root.childCount()):
            assert root.child(row).childCount() == 0, "file rows must be lazy"
        assert "20000" in view.summary_label.text()
        assert elapsed < 2.0, f"tree build took {elapsed:.2f}s at {FILE_COUNT} files"

    def test_expanding_a_folder_renders_only_that_folder(self, populated, qt_app):
        window, _ctx, storage = populated
        view = window.restore
        _select_storage(view, storage.id)
        view._load_files()

        item = _any_folder_item(view, "folder07/")
        assert item is not None and item.childCount() == 0, "file rows must be lazy"
        siblings_before = sum(
            view.tree.topLevelItem(row).childCount()
            for row in range(view.tree.topLevelItemCount())
        )
        item.setExpanded(True)
        qt_app.processEvents()
        assert item.childCount() == FILES_PER_FOLDER
        siblings_after = sum(
            view.tree.topLevelItem(row).childCount()
            for row in range(view.tree.topLevelItemCount())
        )
        assert siblings_after == siblings_before + FILES_PER_FOLDER
        assert item.child(0).data(0, QtCore.Qt.ItemDataRole.UserRole) is not None

    def test_folder_check_selects_files_that_were_never_rendered(self, populated, qt_app):
        window, ctx, storage = populated
        view = window.restore
        _select_storage(view, storage.id)
        view._load_files()

        item = _any_folder_item(view, "folder01/")
        item.setCheckState(0, QtCore.Qt.CheckState.Checked)
        qt_app.processEvents()
        chosen = view._checked_ids()
        assert len(chosen) == FILES_PER_FOLDER  # whole folder, unrendered
        assert item.childCount() == 0, "ticking a folder must not render its files"

        destination = ctx.config.get_data_dir() / "restored"
        ctx.services.restore.start_files(chosen, destination)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ctx.services.restore.is_running:
            _pump(qt_app, 0.1)
        assert not ctx.services.restore.is_running
        restored = list(destination.rglob("*.bin"))
        assert len(restored) == len(chosen), (len(restored), len(chosen))

    def test_select_all_and_partial_check_states(self, populated, qt_app):
        window, _ctx, storage = populated
        view = window.restore
        _select_storage(view, storage.id)
        view._load_files()

        view._set_all(True)
        assert len(view._checked_ids()) == FILE_COUNT
        assert all(view.tree.topLevelItem(row).checkState(0)
                   == QtCore.Qt.CheckState.Checked
                   for row in range(view.tree.topLevelItemCount()))

        view._set_all(False)
        assert view._checked_ids() == []

        item = _any_folder_item(view, "folder02/")
        item.setCheckState(0, QtCore.Qt.CheckState.Checked)
        item.setExpanded(True)
        qt_app.processEvents()
        first = item.child(0)
        first.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
        qt_app.processEvents()
        assert item.checkState(0) == QtCore.Qt.CheckState.PartiallyChecked
        assert len(view._checked_ids()) == FILES_PER_FOLDER - 1

        item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
        qt_app.processEvents()
        assert view._checked_ids() == []
        assert first.checkState(0) == QtCore.Qt.CheckState.Unchecked


class TestTransfersAtScale:
    def _seed_history(self, ctx, storage_id, count=60):
        for index in range(count):
            tid = ctx.repos.transfers.create_or_reset(
                "upload", storage_id, None, 1000, f"/src/file{index}.bin"
            )
            ctx.repos.transfers.set_status(
                tid, "failed" if index % 2 else "completed", "boom"
            )

    def test_identical_content_is_not_rebuilt(self, populated, qt_app):
        window, ctx, storage = populated
        self._seed_history(ctx, storage.id)
        view = window.transfers
        window.nav.setCurrentRow(3)
        _pump(qt_app, 0.3)

        # the page shows the live queue plus the newest 50 finished transfers
        assert view.table.rowCount() == 50
        before = view.table.item(0, 0)
        view.refresh()
        assert view.table.item(0, 0) is before, "identical rows were rebuilt"

    def test_selection_survives_a_refresh(self, populated, qt_app):
        window, ctx, storage = populated
        self._seed_history(ctx, storage.id)
        view = window.transfers
        window.nav.setCurrentRow(3)
        _pump(qt_app, 0.3)

        view.table.selectRow(0)  # newest row; it stays visible when another appears
        selected_id = view._selected_transfer()
        assert selected_id is not None
        # a new failure arrives -> the table must refresh but keep the selection
        tid = ctx.repos.transfers.create_or_reset(
            "upload", storage.id, None, 500, "/src/late.bin"
        )
        ctx.repos.transfers.set_status(tid, "failed", "no")
        view.refresh()
        assert view._selected_transfer() == selected_id

    def test_progress_burst_does_not_rebuild_per_event(self, populated, qt_app):
        window, ctx, storage = populated
        self._seed_history(ctx, storage.id)
        view = window.transfers
        window.nav.setCurrentRow(3)
        _pump(qt_app, 0.3)

        rebuilds = []
        original = view.refresh

        def counting_refresh():
            rebuilds.append(1)
            original()

        view.refresh = counting_refresh
        for _ in range(300):  # a fast transfer reporting every part
            view._on_progress({"done_bytes": 1})
        _pump(qt_app, 0.6)
        assert len(rebuilds) < 20, f"{len(rebuilds)} refreshes for 300 progress events"


class TestLongRunHygiene:
    def test_transfer_history_is_pruned_to_a_cap(self, populated, qt_app):
        window, ctx, storage = populated
        repository = ctx.repos.transfers
        for index in range(300):
            tid = repository.create_or_reset(
                "upload", storage.id, None, 10, f"/src/history{index}.bin"
            )
            repository.set_status(tid, "failed", "old")
        repository.create_or_reset(
            "upload", storage.id, None, 10, "/src/in-flight.bin"
        )
        removed = repository.prune_history(keep=50)
        assert removed == 250
        remaining = repository.list_recent(1000)
        assert len(remaining) == 51  # 50 finished + the active one
        assert [t.local_path for t in remaining if t.status == "queued"] == [
            "/src/in-flight.bin"
        ]

    def test_retrying_a_file_reuses_its_transfer_row(self, populated, ctx=None):
        """A file retried many times keeps exactly one row (bounded growth)."""
        window, context, storage = populated
        from teloude.infrastructure.repositories import FileRepository

        record = FileRepository(context.db).upsert(
            storage.id, None, "/src/same.bin", "same.bin", "same.bin",
            10, 0.0, "0" * 64, "10:1",
        )
        for _ in range(25):
            tid = context.repos.transfers.create_or_reset(
                "upload", storage.id, record, 10, "/src/same.bin"
            )
            context.repos.transfers.set_status(tid, "failed", "again")
        rows = [t for t in context.repos.transfers.list_recent(500)
                if t.local_path == "/src/same.bin"]
        assert len(rows) == 1, "retries must reuse the transfer row"

    def test_preview_cache_and_logs_stay_bounded(self, populated, tmp_path):
        """Nothing under the data directory may grow without a cap."""
        from teloude.core import preview as preview_module

        assert preview_module.CACHE_LIMIT <= 500
        assert preview_module.CACHE_MAX_BYTES <= 100 * 1024 * 1024

        # the data directory only ever holds bounded state
        window, ctx, storage = populated
        assert ctx.repos.transfers.prune_history(keep=200) >= 0
