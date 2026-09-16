# teloude/tests/test_edge_cases.py
"""Boundary conditions: odd file names, unreadable files, hostile destinations.

These are the paths users hit in the real world (locked files, read-only drives,
full disks, non-ASCII names, deep trees) and the ones a backup tool must survive
without losing the rest of the run.
"""
import errno
import os
import pathlib
import shutil

import pytest

from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.restore import RestoreError, RestoreManager
from teloude.core.transfers import TransferRegistry, TransferState
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="chmod-based permission tests are POSIX-only"
)


@pytest.fixture()
def env(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "edge.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    registry = TransferRegistry(TransferRepository(manager))
    storage_gw, file_gw = FakeStorageGateway(), FakeFileGateway()
    backup = BackupManager(storages, folders, files, registry, storage_gw, file_gw,
                           max_retries=2, retry_sleeper=lambda _s: None)
    restore = RestoreManager(files, registry, file_gw, max_retries=1)
    yield {
        "manager": manager, "storages": storages, "files": files,
        "registry": registry, "backup": backup, "restore": restore,
        "file_gw": file_gw, "tmp": tmp_path,
    }
    close_db_connection(manager)


def _link(env, name):
    info = env["backup"]._storage.create_storage(name)
    sid = env["storages"].create(name)
    env["storages"].set_telegram(sid, info.chat_id, True)
    return sid


def _run_backup(env, sid, root):
    return env["backup"].run(env["backup"].plan(sid, root))


class TestHostileNames:
    def test_unicode_long_and_deep_paths_round_trip(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        interesting = [
            "سلام-دنیا.txt",
            "日本語ファイル.txt",
            "emoji 🎉😀.bin",
            "Ünïcödé-ÄÖÜ-Ñ.txt",
            "spaces and 'quotes' and #hash.txt",
            "dot.in.name.tar.gz",
            "a" * 180 + ".txt",
        ]
        for name in interesting:
            (source / name).write_bytes(("payload:" + name).encode("utf-8"))

        deep = source
        for depth in range(25):
            deep = deep / f"level{depth}"
        deep.mkdir(parents=True)
        target = deep / "deep.txt"
        target.write_bytes(b"deep payload")

        sid = _link(env, "Names")
        report = _run_backup(env, sid, source)
        assert report.failed == []
        assert report.uploaded == len(interesting) + 1

        rows = [f for f in env["files"].list_by_storage(sid) if f.is_backed_up]
        assert len(rows) == len(interesting) + 1
        # names survive the index unchanged
        stored_names = {pathlib.Path(r.relative_path).name for r in rows}
        assert stored_names == set(interesting) | {"deep.txt"}

        destination = tmp_path / "restored"
        result = env["restore"].restore_files(rows, destination)
        assert result.failed == [] and result.restored == len(rows)
        for row in rows:
            original = source / row.relative_path
            copy = destination / row.relative_path
            assert copy.exists(), row.relative_path
            assert copy.read_bytes() == original.read_bytes(), row.relative_path


class TestUnreadableFiles:
    @POSIX_ONLY
    def test_unreadable_file_does_not_sink_the_run(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "readable.txt").write_bytes(b"fine")
        locked = source / "locked.bin"
        locked.write_bytes(b"cannot read me")
        os.chmod(locked, 0)
        try:
            sid = _link(env, "Perms")
            report = _run_backup(env, sid, source)
            assert report.uploaded == 1, report
            assert [path for path, _ in report.failed] == ["locked.bin"]
            message = report.failed[0][1].lower()
            assert "permission denied" in message
            assert "network" not in message  # the real cause, not a red herring
            rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
            assert rows["readable.txt"].is_backed_up
            assert "locked.bin" not in rows or not rows["locked.bin"].is_backed_up
        finally:
            os.chmod(locked, 0o600)

    @POSIX_ONLY
    def test_unreadable_folder_is_skipped_not_fatal(self, env, tmp_path):
        source = tmp_path / "src"
        (source / "good").mkdir(parents=True)
        (source / "good" / "a.txt").write_bytes(b"a")
        blocked = source / "blocked"
        blocked.mkdir()
        (blocked / "hidden.txt").write_bytes(b"hidden")
        os.chmod(blocked, 0)
        try:
            sid = _link(env, "Folders")
            report = _run_backup(env, sid, source)
            assert report.uploaded == 1, report
            assert report.failed == []  # an unreadable folder is skipped silently
        finally:
            os.chmod(blocked, 0o700)


class TestRestoreDestinations:
    def _backed_up(self, env, tmp_path, payload=b"payload-bytes"):
        source = tmp_path / "src"
        source.mkdir()
        (source / "file.bin").write_bytes(payload)
        sid = _link(env, "Dest")
        _run_backup(env, sid, source)
        return [f for f in env["files"].list_by_storage(sid) if f.is_backed_up]

    def test_destination_that_is_a_file_is_refused_clearly(self, env, tmp_path):
        rows = self._backed_up(env, tmp_path)
        blocker = tmp_path / "not-a-folder"
        blocker.write_bytes(b"i am a file")
        with pytest.raises(RestoreError) as excinfo:
            env["restore"].restore_files(rows, blocker)
        message = str(excinfo.value)
        assert "is a file, not a folder" in message
        assert blocker.read_bytes() == b"i am a file", "the file must not be touched"

    def test_parent_path_blocked_by_a_file_is_reported_per_file(self, env, tmp_path):
        source = tmp_path / "src"
        (source / "sub").mkdir(parents=True)
        (source / "sub" / "nested.txt").write_bytes(b"nested")
        sid = _link(env, "Nested")
        _run_backup(env, sid, source)
        rows = [f for f in env["files"].list_by_storage(sid) if f.is_backed_up]

        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "sub").write_bytes(b"file in the way")
        result = env["restore"].restore_files(rows, destination)
        assert result.restored == 0 and len(result.failed) == 1
        assert (destination / "sub").read_bytes() == b"file in the way"
        assert "'sub' is a file" in result.failed[0][1]

    @POSIX_ONLY
    def test_read_only_destination_is_reported_as_permission_not_network(self, env, tmp_path):
        rows = self._backed_up(env, tmp_path)
        locked = tmp_path / "ro"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            result = env["restore"].restore_files(rows, locked)
            assert result.restored == 0 and len(result.failed) == 1
            message = result.failed[0][1].lower()
            assert "permission denied" in message, message
            assert "network" not in message, message
        finally:
            os.chmod(locked, 0o700)

    def test_write_error_midway_is_not_called_a_network_problem(self, env, tmp_path):
        rows = self._backed_up(env, tmp_path)
        env["file_gw"].fail_next_download_with = PermissionError(
            errno.EACCES, "Permission denied"
        )
        result = env["restore"].restore_files(rows, tmp_path / "out")
        assert result.restored == 0 and len(result.failed) == 1
        message = result.failed[0][1].lower()
        assert "permission denied" in message and "network" not in message
        # the transfer row says failed, so the transfers page can offer a retry
        transfer = env["registry"]._repo.list_recent()[0]
        assert transfer.status == TransferState.FAILED.value

    def test_disk_full_is_reported_as_disk_space(self, env, tmp_path, monkeypatch):
        rows = self._backed_up(env, tmp_path)
        env["file_gw"].fail_next_download_with = OSError(errno.ENOSPC, "No space left")
        result = env["restore"].restore_files(rows, tmp_path / "out")
        assert result.restored == 0 and len(result.failed) == 1
        message = result.failed[0][1].lower()
        assert "no space left" in message and "network" not in message

    def test_not_enough_space_is_caught_before_downloading(self, env, tmp_path, monkeypatch):
        rows = self._backed_up(env, tmp_path)

        class _NoSpace:
            free = 1

        monkeypatch.setattr(shutil, "disk_usage", lambda _p: _NoSpace())
        result = env["restore"].restore_files(rows, tmp_path / "out")
        assert result.restored == 0 and len(result.failed) == 1
        assert "not enough disk space" in result.failed[0][1].lower()
        # nothing half-written was left behind
        assert not (tmp_path / "out" / "file.bin").exists()

    def test_network_error_is_still_treated_as_network(self, env, tmp_path):
        """The new local-failure handling must not swallow real outages."""
        rows = self._backed_up(env, tmp_path)
        from teloude.infrastructure.telegram.exceptions import ConnectionStateError

        env["file_gw"].fail_next_download_with = ConnectionStateError("offline")
        result = env["restore"].restore_files(rows, tmp_path / "out")
        assert result.restored == 1, result.failed  # retried successfully
        assert result.failed == []


class TestSourceChangesBetweenScanAndUpload:
    """A backup must survive the user editing files while it runs."""

    def _plan_then(self, env, source, change):
        sid = _link(env, "Live")
        plan = env["backup"].plan(sid, source)  # hashes the original content
        change()
        return sid, env["backup"].run(plan)

    def test_deleted_file_fails_alone(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "keep.txt").write_bytes(b"keep me")
        (source / "gone.txt").write_bytes(b"delete me")
        sid, report = self._plan_then(
            env, source, lambda: (source / "gone.txt").unlink()
        )
        assert report.uploaded == 1, report
        assert [path for path, _ in report.failed] == ["gone.txt"]
        assert "vanished" in report.failed[0][1].lower()
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert rows["keep.txt"].is_backed_up

    def test_modified_file_is_rehashed_and_not_stored_stale(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        target = source / "changing.txt"
        target.write_bytes(b"old content")
        sid, report = self._plan_then(
            env, source, lambda: target.write_bytes(b"new content, longer")
        )
        assert report.uploaded == 1 and report.failed == [], report
        row = env["files"].get_by_path(sid, "changing.txt")
        assert row is not None and row.is_backed_up
        assert row.size == len(b"new content, longer")
        import hashlib

        assert row.sha256 == hashlib.sha256(b"new content, longer").hexdigest()

        # the stored document byte-for-byte matches the file on disk now
        stored = list(env["file_gw"]._blobs.values())[-1]
        assert stored == b"new content, longer"

    def test_file_replaced_by_a_directory_is_reported_clearly(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        victim = source / "thing"
        victim.write_bytes(b"i was a file")
        sid = _link(env, "Replaced")
        plan = env["backup"].plan(sid, source)
        victim.unlink()
        victim.mkdir()  # now a folder sits where the file was
        report = env["backup"].run(plan)
        assert report.uploaded == 0 and len(report.failed) == 1
        message = report.failed[0][1].lower()
        assert "folder" in message or "directory" in message, message
