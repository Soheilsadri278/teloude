# teloude/tests/test_root_folder_restore.py
"""Bug 1 regression tests: restoring must give the selected folder back.

The Windows acceptance test backed up a folder and restored the storage into
D:\\Restored\\, which produced D:\\Restored\\file1.txt and
D:\\Restored\\Subfolder\\file3.pdf - the top level was flattened because the
stored path was relative to the folder the user picked, not to the storage.

Everything below pins the fixed behaviour, including the parts the report asked
to preserve: single-file restore, the four collision choices, and the
path-traversal guard.
"""
import hashlib

import pytest

from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.restore import (
    CollisionAction,
    CollisionDecision,
    RestoreManager,
)
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRecord,
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway


@pytest.fixture()
def env(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "restore.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    storage_gw, file_gw = FakeStorageGateway(), FakeFileGateway()
    backup = BackupManager(
        storages, folders, files, TransferRegistry(TransferRepository(manager)),
        storage_gw, file_gw, max_retries=1, retry_sleeper=lambda _s: None,
    )
    restore = RestoreManager(files, TransferRegistry(TransferRepository(manager)),
                             file_gw, max_retries=1)
    env = {
        "manager": manager, "storages": storages, "folders": folders,
        "files": files, "backup": backup, "restore": restore,
        "storage_gw": storage_gw, "file_gw": file_gw, "tmp": tmp_path,
    }
    yield env
    close_db_connection(manager)


def _linked_storage(env, name="Photos"):
    info = env["storage_gw"].create_storage(name)
    sid = env["storages"].create(name)
    env["storages"].set_telegram(sid, info.chat_id, True)
    return sid


def _backup_tree(tmp_path, name, entries):
    """Creates <tmp_path>/<name>/... from {relative path: payload} and backs it up."""
    root = tmp_path / name
    root.mkdir(parents=True)
    for relative, payload in entries.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


class TestRestoreKeepsTheRootFolder:
    def test_the_restored_tree_mirrors_the_source_folder(self, env, tmp_path):
        """The exact Windows scenario: top-level files must keep their folder."""
        sid = _linked_storage(env, "Backup")
        source = _backup_tree(tmp_path, "MyStuff", {
            "file1.txt": b"one",
            "Subfolder/file3.pdf": b"three",
        })
        report = env["backup"].run(env["backup"].plan(sid, source))
        assert report.uploaded == 2 and report.failed == []

        destination = tmp_path / "Restored"
        records = env["files"].list_by_storage(sid)
        result = env["restore"].restore_files(records, destination)
        assert (result.restored, result.failed) == (2, [])
        # no flattening: everything lives under the folder the user selected
        assert (destination / "MyStuff" / "file1.txt").read_bytes() == b"one"
        assert (destination / "MyStuff" / "Subfolder" / "file3.pdf").read_bytes() == b"three"
        assert not (destination / "file1.txt").exists()

    def test_a_deep_hierarchy_survives_the_round_trip(self, env, tmp_path):
        sid = _linked_storage(env, "Deep")
        source = _backup_tree(tmp_path, "Project", {
            "a.txt": b"a",
            "docs/2026/q1/report.md": b"report",
            "docs/2026/q1/data/raw.bin": b"raw",
        })
        env["backup"].run(env["backup"].plan(sid, source))
        destination = tmp_path / "out"
        env["restore"].restore_files(env["files"].list_by_storage(sid), destination)
        for relative in ("a.txt", "docs/2026/q1/report.md", "docs/2026/q1/data/raw.bin"):
            restored = destination / "Project" / relative
            assert restored.exists(), relative
            assert restored.read_bytes() == (source / relative).read_bytes(), relative

    def test_single_file_restore_keeps_its_place_in_the_tree(self, env, tmp_path):
        sid = _linked_storage(env, "Single")
        source = _backup_tree(tmp_path, "Notes", {
            "top.txt": b"top",
            "sub/one.txt": b"one",
            "sub/two.txt": b"two",
        })
        env["backup"].run(env["backup"].plan(sid, source))
        record = env["files"].get_by_path(sid, "Notes/sub/one.txt")
        destination = tmp_path / "single"
        result = env["restore"].restore_files([record], destination)
        assert result.restored == 1
        assert (destination / "Notes" / "sub" / "one.txt").read_bytes() == b"one"
        # nothing else was restored, and no empty sibling folders were invented
        assert not (destination / "Notes" / "sub" / "two.txt").exists()
        assert not (destination / "Notes" / "top.txt").exists()

    def test_two_root_folders_restore_into_their_own_trees(self, env, tmp_path):
        """Same file name in two root folders: two trees, no collision prompt."""
        sid = _linked_storage(env, "Both")
        parent = tmp_path / "sources"
        parent.mkdir()
        left = _backup_tree(parent, "FolderA", {"shared.txt": b"from A"})
        right = _backup_tree(parent, "FolderB", {"shared.txt": b"from B"})
        assert env["backup"].run(env["backup"].plan(sid, left)).uploaded == 1
        assert env["backup"].run(env["backup"].plan(sid, right)).uploaded == 1

        destination = tmp_path / "out"
        prompts = []
        result = env["restore"].restore_files(
            env["files"].list_by_storage(sid), destination,
            collision_callback=lambda *a: prompts.append(a) or
            CollisionDecision(CollisionAction.OVERWRITE),
        )
        assert result.restored == 2 and prompts == []  # nothing collided
        assert (destination / "FolderA" / "shared.txt").read_bytes() == b"from A"
        assert (destination / "FolderB" / "shared.txt").read_bytes() == b"from B"


class TestCollisionHandlingStillWorks:
    """The collision flow must keep working on the new (nested) target path."""

    def _backed_up(self, env, tmp_path, payload=b"fresh content"):
        sid = _linked_storage(env, "Collide")
        source = _backup_tree(tmp_path, "Folder", {"doc.txt": payload})
        env["backup"].run(env["backup"].plan(sid, source))
        return sid, env["files"].get_by_path(sid, "Folder/doc.txt")

    def test_identical_file_is_skipped_without_asking(self, env, tmp_path):
        _, record = self._backed_up(env, tmp_path, b"same bytes")
        destination = tmp_path / "out"
        (destination / "Folder").mkdir(parents=True)
        (destination / "Folder" / "doc.txt").write_bytes(b"same bytes")
        asked = []
        result = env["restore"].restore_files(
            [record], destination, collision_callback=lambda *a: asked.append(a),
        )
        assert result.skipped == 1 and result.restored == 0 and asked == []

    def test_keep_both_writes_beside_the_existing_file(self, env, tmp_path):
        _, record = self._backed_up(env, tmp_path, b"new bytes")
        destination = tmp_path / "out"
        (destination / "Folder").mkdir(parents=True)
        (destination / "Folder" / "doc.txt").write_bytes(b"old bytes")
        result = env["restore"].restore_files(
            [record], destination,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.KEEP_BOTH),
        )
        assert result.restored == 1
        assert (destination / "Folder" / "doc.txt").read_bytes() == b"old bytes"
        assert (destination / "Folder" / "doc (2).txt").read_bytes() == b"new bytes"

    def test_overwrite_replaces_the_file_in_place(self, env, tmp_path):
        _, record = self._backed_up(env, tmp_path, b"new bytes")
        destination = tmp_path / "out"
        (destination / "Folder").mkdir(parents=True)
        (destination / "Folder" / "doc.txt").write_bytes(b"old bytes")
        result = env["restore"].restore_files(
            [record], destination,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.OVERWRITE),
        )
        assert result.restored == 1
        assert (destination / "Folder" / "doc.txt").read_bytes() == b"new bytes"

    def test_cancel_stops_the_restore_before_writing(self, env, tmp_path):
        sid, record = self._backed_up(env, tmp_path)
        destination = tmp_path / "out"
        (destination / "Folder").mkdir(parents=True)
        (destination / "Folder" / "doc.txt").write_bytes(b"old bytes")
        result = env["restore"].restore_files(
            [record], destination,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.CANCEL),
        )
        assert result.cancelled is True and result.restored == 0
        assert (destination / "Folder" / "doc.txt").read_bytes() == b"old bytes"

    def test_path_traversal_is_still_refused(self, env, tmp_path):
        destination = tmp_path / "out"
        evil = FileRecord(
            id=99, storage_id=1, folder_id=None, local_path="/nowhere",
            relative_path="Folder/../../escape.txt", file_name="escape.txt",
            size=4, is_backed_up=True, telegram_chat_id=-1, telegram_msg_id=1,
        )
        env["file_gw"]._blobs[1] = b"evil"
        result = env["restore"].restore_files([evil], destination)
        assert len(result.failed) == 1
        assert not (tmp_path / "escape.txt").exists()
        assert not (destination.parent / "escape.txt").exists()


class TestPreFixIndexIsReAnchored:
    """An index written before the fix must not restore a flattened tree.

    After upgrading, the rows of the previous version still say "file1.txt"
    instead of "MyStuff/file1.txt". Re-running the same backup adopts them (no
    re-upload, no lost cloud copy), so restore immediately gives the folder back.
    """

    def _legacy_backup(self, env, tmp_path, root_name="MyStuff"):
        sid = _linked_storage(env, "Legacy")
        source = _backup_tree(tmp_path, root_name, {
            "file1.txt": b"legacy one",
            "Subfolder/file3.pdf": b"legacy three",
        })
        chat_id = env["storages"].get(sid).telegram_chat_id
        for relative, payload in (
            ("file1.txt", b"legacy one"),
            ("Subfolder/file3.pdf", b"legacy three"),
        ):
            local = source / relative
            stat = local.stat()
            row_id = env["files"].upsert(
                sid, None, str(local), relative, local.name, len(payload),
                stat.st_mtime, hashlib.sha256(payload).hexdigest(), None,
            )
            uploaded = env["file_gw"].upload(local)
            env["file_gw"].send_to_topic(chat_id, 1, uploaded)
            env["files"].mark_backed_up(row_id, chat_id, uploaded.file_id)
        return sid, source

    def test_the_next_backup_moves_them_under_the_root_folder(self, env, tmp_path):
        sid, source = self._legacy_backup(env, tmp_path)
        before = {r.relative_path for r in env["files"].list_by_storage(sid)}
        assert before == {"file1.txt", "Subfolder/file3.pdf"}  # the old shape

        plan = env["backup"].plan(sid, source)
        assert sorted(plan.unchanged) == ["MyStuff/Subfolder/file3.pdf", "MyStuff/file1.txt"]
        report = env["backup"].run(plan)
        # adopted, not re-uploaded: the cloud copies stay exactly where they were
        assert (report.uploaded, report.failed) == (0, [])
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert set(rows) == {"MyStuff/file1.txt", "MyStuff/Subfolder/file3.pdf"}
        assert all(r.is_backed_up and r.telegram_msg_id for r in rows.values())
        assert len(env["file_gw"].sent) == 2  # nothing was posted a second time

    def test_the_adopted_rows_restore_under_the_root_folder(self, env, tmp_path):
        sid, source = self._legacy_backup(env, tmp_path)
        env["backup"].run(env["backup"].plan(sid, source))
        destination = tmp_path / "restored"
        result = env["restore"].restore_files(
            env["files"].list_by_storage(sid), destination
        )
        assert (result.restored, result.failed) == (2, [])
        assert (destination / "MyStuff" / "file1.txt").read_bytes() == b"legacy one"
        assert (destination / "MyStuff" / "Subfolder" / "file3.pdf").read_bytes() == \
            b"legacy three"

    def test_rows_outside_the_selected_root_are_left_alone(self, env, tmp_path):
        sid, source = self._legacy_backup(env, tmp_path)
        other = _backup_tree(tmp_path / "elsewhere", "Other", {"keep.txt": b"keep"})
        stat = (other / "keep.txt").stat()
        outsider = env["files"].upsert(
            sid, None, str(other / "keep.txt"), "keep.txt", "keep.txt",
            len(b"keep"), stat.st_mtime,
            hashlib.sha256(b"keep").hexdigest(), None,
        )
        env["backup"].run(env["backup"].plan(sid, source))
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert "Other/keep.txt" not in rows           # not inside the selected root
        assert rows["keep.txt"].id == outsider        # untouched, still there
        assert "MyStuff/file1.txt" in rows
