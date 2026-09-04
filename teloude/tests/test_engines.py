# teloude/tests/test_engines.py
"""Backup + restore engine tests over fake Telegram gateways (no network)."""
import threading

import pytest

from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.control import EngineControl
from teloude.core.duplicates import DuplicateResolver
from teloude.core.restore import (
    CollisionAction,
    CollisionDecision,
    RestoreManager,
    keep_both_path,
    safe_destination,
)
from teloude.core.restore import RestoreError
from teloude.core.transfers import TransferRegistry, TransferState
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway


@pytest.fixture()
def env(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "e.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    transfers = TransferRepository(manager)
    registry = TransferRegistry(transfers)
    storage_gw = FakeStorageGateway()
    file_gw = FakeFileGateway()
    backup = BackupManager(
        storages, folders, files, registry, storage_gw, file_gw,
        max_retries=2, retry_sleeper=lambda s: None,
    )
    restore = RestoreManager(files, registry, file_gw, max_retries=2)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"alpha")
    (src / "sub").mkdir()
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 4)
    yield {
        "manager": manager, "storages": storages, "folders": folders,
        "files": files, "registry": registry, "backup": backup,
        "restore": restore, "storage_gw": storage_gw, "file_gw": file_gw,
        "src": src, "tmp": tmp_path,
    }
    close_db_connection(manager)


def _link_storage(env, name="Photos"):
    info = env["storage_gw"].create_storage(name)
    sid = env["storages"].create(name)
    env["storages"].set_telegram(sid, info.chat_id, True)
    return sid, info


class TestBackup:
    def test_plan_hashes_and_indexes(self, env):
        sid, _ = _link_storage(env)
        plan = env["backup"].plan(sid, env["src"])
        assert len(plan.files) == 2
        assert all(f.sha256 for f in plan.files)
        assert plan.duplicates == []
        assert len(env["files"].list_by_storage(sid)) == 2

    def test_full_run_marks_backed_up(self, env):
        sid, info = _link_storage(env)
        plan = env["backup"].plan(sid, env["src"])
        report = env["backup"].run(plan)
        assert (report.uploaded, report.failed, report.cancelled) == (2, [], False)
        assert all(f.is_backed_up for f in env["files"].list_by_storage(sid))
        topics = {t.title for t in env["storage_gw"].list_topics(info.chat_id)}
        assert {"Photos", "Photos / sub"} <= topics
        stats = env["storages"].get(sid)
        assert stats.file_count == 2 and stats.total_size > 0

    def test_duplicate_skipped(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        (env["src"] / "copy_of_a.txt").write_bytes(b"alpha")  # same content, new path
        plan = env["backup"].plan(sid, env["src"])
        assert len(plan.duplicates) == 1
        report = env["backup"].run(
            plan, resolver=DuplicateResolver(policy=DuplicateResolver.SKIP_ALL)
        )
        assert report.skipped_duplicates == 1 and report.uploaded == 2

    def test_duplicate_upload_again(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        (env["src"] / "copy_of_a.txt").write_bytes(b"alpha")
        plan = env["backup"].plan(sid, env["src"])
        report = env["backup"].run(
            plan, resolver=DuplicateResolver(policy=DuplicateResolver.UPLOAD_ALL)
        )
        assert report.uploaded == 3 and report.skipped_duplicates == 0

    def test_network_blip_retries(self, env):
        sid, _ = _link_storage(env)
        env["file_gw"].fail_next_upload_with = OSError("net down")
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.uploaded == 2 and report.failed == []

    def test_pause_before_start_waits_for_resume(self, env):
        sid, _ = _link_storage(env)
        control = EngineControl()
        control.pause()
        threading.Timer(0.5, control.resume).start()
        report = env["backup"].run(
            env["backup"].plan(sid, env["src"]), control=control
        )
        assert report.uploaded == 2 and report.failed == []

    def test_pause_mid_upload_resumes_from_checkpoint(self, env):
        sid, _ = _link_storage(env)
        (env["src"] / "big.bin").write_bytes(b"b" * 200000)  # 4 x 64KiB parts

        class PausingControl(EngineControl):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def should_pause(self):
                self.calls += 1
                return self.calls == 2  # pause once, right before part 1

        plan = env["backup"].plan(sid, env["src"])
        report = env["backup"].run(plan, control=PausingControl())
        assert report.uploaded == 3 and report.failed == []
        # Checkpoint resume: plan order is a.txt, big.bin, sub/b.bin, so the
        # 4 parts of big.bin sit at indices 1..4 exactly once, in order.
        assert env["file_gw"].uploaded_parts[1:5] == [0, 1, 2, 3]

    def test_cancel_aborts_run(self, env):
        sid, _ = _link_storage(env)
        control = EngineControl()
        control.cancel()
        report = env["backup"].run(env["backup"].plan(sid, env["src"]), control=control)
        assert report.cancelled is True and report.uploaded == 0

    def test_missing_source_fails_file_not_run(self, env):
        sid, _ = _link_storage(env)
        plan = env["backup"].plan(sid, env["src"])
        (env["src"] / "a.txt").unlink()
        report = env["backup"].run(plan)
        assert report.uploaded == 1
        assert len(report.failed) == 1 and "a.txt" in report.failed[0][0]

    def test_oversized_file_rejected(self, env):
        sid, _ = _link_storage(env)
        env["file_gw"]._max_bytes = 4
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.uploaded == 0 and len(report.failed) == 2

    def test_recover_pending(self, env):
        sid, info = _link_storage(env)
        env["backup"].plan(sid, env["src"])  # indexed, not yet uploaded
        rec = env["files"].get_by_path(sid, "a.txt")
        assert rec.is_backed_up is False
        tid = env["registry"].start_upload(rec.id, sid, rec.size, rec.local_path).id
        env["registry"].transition(tid, TransferState.UPLOADING)
        env["registry"].checkpoint(tid, 2)
        assert env["backup"].recover_pending() == 1
        assert env["registry"].active_record(tid).status == "queued"
        # Vanished source -> failed, not requeued.
        (env["src"] / "sub" / "b.bin").unlink()
        rec2 = env["files"].get_by_path(sid, "sub/b.bin")
        tid2 = env["registry"].start_upload(rec2.id, sid, rec2.size, rec2.local_path).id
        env["registry"].transition(tid2, TransferState.UPLOADING)
        assert env["backup"].recover_pending() == 1  # only the intact a.txt
        assert env["registry"].active_record(tid2).status == "failed"
        # Already-backed-up rows are stale, never re-uploaded.
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        tid3 = env["registry"].start_upload(rec.id, sid, rec.size, rec.local_path).id
        env["registry"].transition(tid3, TransferState.UPLOADING)
        assert env["backup"].recover_pending() == 0
        assert env["registry"].active_record(tid3).status == "failed"


class TestRestore:
    def _backed_up(self, env):
        sid, _ = _link_storage(env)
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.failed == []
        return sid

    def test_roundtrip(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        records = env["files"].list_by_storage(sid)
        report = env["restore"].restore_files(records, dest)
        assert (report.restored, report.failed) == (2, [])
        assert (dest / "a.txt").read_bytes() == b"alpha"
        assert (dest / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 4

    def test_identical_existing_skipped_silently(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"alpha")
        calls = []
        records = [env["files"].get_by_path(sid, "a.txt")]
        report = env["restore"].restore_files(
            records, dest, collision_callback=lambda *a: calls.append(a) or CollisionDecision(CollisionAction.OVERWRITE),
        )
        assert report.skipped == 1 and calls == []

    def test_collision_overwrite_and_keep_both(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"different-content!")
        records = [env["files"].get_by_path(sid, "a.txt")]
        report = env["restore"].restore_files(
            records, dest,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.KEEP_BOTH),
        )
        assert report.restored == 1
        assert (dest / "a (2).txt").read_bytes() == b"alpha"
        assert (dest / "a.txt").read_bytes() == b"different-content!"

    def test_collision_cancel(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"x")
        records = env["files"].list_by_storage(sid)
        report = env["restore"].restore_files(
            records, dest,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.CANCEL),
        )
        assert report.cancelled is True and report.restored == 0

    def test_traversal_rejected(self, env):
        from teloude.infrastructure.repositories import FileRecord

        dest = env["tmp"] / "out"
        evil = FileRecord(
            id=1, storage_id=1, folder_id=None, local_path="/x", relative_path="../../evil.txt",
            file_name="evil.txt", size=1, is_backed_up=True,
            telegram_chat_id=-1001, telegram_msg_id=1,
        )
        report = env["restore"].restore_files([evil], dest)
        assert len(report.failed) == 1
        assert not (env["tmp"] / "evil.txt").exists()

    def test_integrity_failure_removes_partial(self, env):
        sid = self._backed_up(env)
        rec = env["files"].get_by_path(sid, "a.txt")
        env["file_gw"]._blobs[rec.telegram_msg_id] = b"tampered!"
        dest = env["tmp"] / "out"
        report = env["restore"].restore_files([rec], dest)
        assert len(report.failed) == 1
        assert not (dest / "a.txt").exists()

    def test_unbacked_file_fails(self, env):
        sid, _ = _link_storage(env)
        plan = env["backup"].plan(sid, env["src"])  # indexed but not uploaded
        rec = env["files"].get_by_path(sid, "a.txt")
        assert rec.is_backed_up is False
        report = env["restore"].restore_files([rec], env["tmp"] / "out")
        assert len(report.failed) == 1


class TestRestoreHelpers:
    def test_safe_destination(self, tmp_path):
        assert safe_destination(tmp_path, "a/b.txt") == tmp_path / "a" / "b.txt"
        with pytest.raises(RestoreError):
            safe_destination(tmp_path, "../escape.txt")
        with pytest.raises(RestoreError):
            safe_destination(tmp_path, "/absolute.txt")

    def test_keep_both_naming(self, tmp_path):
        target = tmp_path / "doc.pdf"
        target.write_bytes(b"1")
        second = keep_both_path(target)
        assert second.name == "doc (2).pdf"
        second.write_bytes(b"2")
        assert keep_both_path(target).name == "doc (3).pdf"
