# teloude/tests/test_engines.py
"""Backup + restore engine tests over fake Telegram gateways (no network)."""
import os
import threading

import pytest

from teloude.core import scanner as scanner_module

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
    # The selected root folder is the anchor of every stored relative path,
    # so this tree is indexed as "src/a.txt" and "src/sub/b.bin" (Bug 1/Bug 4).
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
        assert {"Photos / src", "Photos / src / sub"} <= topics
        stats = env["storages"].get(sid)
        assert stats.file_count == 2 and stats.total_size > 0

    def test_duplicate_skipped(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        (env["src"] / "copy_of_a.txt").write_bytes(b"alpha")  # same content, new path
        plan = env["backup"].plan(sid, env["src"])
        assert len(plan.duplicates) == 1
        assert sorted(plan.unchanged) == ["src/a.txt", "src/sub/b.bin"]  # untouched
        report = env["backup"].run(
            plan, resolver=DuplicateResolver(policy=DuplicateResolver.SKIP_ALL)
        )
        assert report.skipped_duplicates == 1 and report.uploaded == 0
        assert report.unchanged == 2 and report.skipped == 3

    def test_duplicate_upload_again(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        (env["src"] / "copy_of_a.txt").write_bytes(b"alpha")
        plan = env["backup"].plan(sid, env["src"])
        report = env["backup"].run(
            plan, resolver=DuplicateResolver(policy=DuplicateResolver.UPLOAD_ALL)
        )
        # only the new copy is uploaded; the untouched files stay skipped
        assert report.uploaded == 1 and report.skipped_duplicates == 0
        assert report.unchanged == 2

    def test_unchanged_files_are_not_read_again(self, env, monkeypatch):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        reads = []
        real_sha = scanner_module.sha256_of

        def counting_sha(path, *args, **kwargs):
            reads.append(str(path))
            return real_sha(path, *args, **kwargs)

        monkeypatch.setattr(scanner_module, "sha256_of", counting_sha)
        plan = env["backup"].plan(sid, env["src"])
        assert sorted(plan.unchanged) == ["src/a.txt", "src/sub/b.bin"]
        assert reads == []  # size + mtime matched: no bytes were re-read

        forced = env["backup"].plan(sid, env["src"], verify_content=True)
        assert sorted(forced.unchanged) == ["src/a.txt", "src/sub/b.bin"]
        assert len(reads) == 2  # the override re-hashes everything

    def test_edit_that_keeps_size_and_mtime_needs_the_override(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        target = env["src"] / "a.txt"
        stat = target.stat()
        target.write_bytes(b"ALPHA")  # same size, different content
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # hide the edit

        plan = env["backup"].plan(sid, env["src"])
        assert "src/a.txt" in plan.unchanged  # fast path: size + mtime match
        assert "src/sub/b.bin" in plan.unchanged

        verified = env["backup"].plan(sid, env["src"], verify_content=True)
        assert "src/a.txt" not in verified.unchanged
        assert "src/a.txt" in verified.superseded
        assert verified.unchanged == ["src/sub/b.bin"]

    def test_changed_file_is_replaced_and_old_copy_removed(self, env):
        sid, _ = _link_storage(env)
        first = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert first.uploaded == 2
        old_row = env["files"].get_by_path(sid, "src/a.txt")
        (env["src"] / "a.txt").write_bytes(b"alpha v2, longer")
        plan = env["backup"].plan(sid, env["src"])
        assert plan.unchanged == ["src/sub/b.bin"]  # b.bin untouched
        assert plan.superseded["src/a.txt"] == (old_row.telegram_chat_id, old_row.telegram_msg_id)
        report = env["backup"].run(plan)
        assert report.uploaded == 1 and report.unchanged == 1
        assert env["file_gw"].deleted[-1][1] == (old_row.telegram_msg_id,)
        fresh = env["files"].get_by_path(sid, "src/a.txt")
        assert fresh.telegram_msg_id and fresh.telegram_msg_id != old_row.telegram_msg_id
        assert fresh.is_backed_up

    def test_changed_file_keeps_old_copy_when_upload_fails(self, env):
        from teloude.infrastructure.telegram.exceptions import ConnectionStateError

        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        (env["src"] / "a.txt").write_bytes(b"alpha v2, longer")
        plan = env["backup"].plan(sid, env["src"])

        def always_fails(*_args, **_kwargs):
            raise ConnectionStateError("permanent outage")

        env["file_gw"].upload = always_fails
        report = env["backup"].run(plan)
        assert [rel for rel, _ in report.failed] == ["src/a.txt"]
        # the old cloud copy must survive a failed replacement
        assert env["file_gw"].deleted == []

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

    def test_pause_resume_uploads_every_part_exactly_once(self, env):
        """Regression: a resumed upload must keep Telegram's file id.

        Losing it made the resumed attempt write parts under a fresh id, so the
        document Telegram assembled was missing its first parts.
        """
        sid, _ = _link_storage(env)
        payload = bytes(range(256)) * 1000  # ~250 KiB -> 4 parts at 64 KiB
        (env["src"] / "big.bin").write_bytes(payload)

        class PausingControl(EngineControl):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def should_pause(self):
                self.calls += 1
                return self.calls == 2  # once, right before part 1

        plan = env["backup"].plan(sid, env["src"])
        report = env["backup"].run(plan, control=PausingControl())
        assert report.uploaded == 3 and report.failed == []

        big_parts = [p for p in env["file_gw"].uploaded_parts][1:5]
        assert big_parts == [0, 1, 2, 3]  # every part once, in order
        row = [
            t for t in env["registry"]._repo.list_recent()
            if t.kind == "upload" and t.local_path.endswith("big.bin")
        ][0]
        assert row.status == TransferState.COMPLETED.value
        assert row.done_bytes == len(payload)  # progress never double counts
        document = [
            blob for blob in env["file_gw"]._blobs.values() if len(blob) == len(payload)
        ]
        assert document == [payload], "the resumed upload produced a broken document"

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
        assert len(report.failed) == 1 and "src/a.txt" in report.failed[0][0]

    def test_oversized_file_rejected(self, env):
        sid, _ = _link_storage(env)
        env["file_gw"]._max_bytes = 4
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.uploaded == 0 and len(report.failed) == 2

    def test_recover_pending(self, env):
        sid, info = _link_storage(env)
        env["backup"].plan(sid, env["src"])  # indexed, not yet uploaded
        rec = env["files"].get_by_path(sid, "src/a.txt")
        assert rec.is_backed_up is False
        tid = env["registry"].start_upload(rec.id, sid, rec.size, rec.local_path).id
        env["registry"].transition(tid, TransferState.UPLOADING)
        env["registry"].checkpoint(tid, 2)
        assert env["backup"].recover_pending() == 1
        assert env["registry"].active_record(tid).status == "queued"
        # Vanished source -> failed, not requeued.
        (env["src"] / "sub" / "b.bin").unlink()
        rec2 = env["files"].get_by_path(sid, "src/sub/b.bin")
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
        # Bug 1: the selected root folder is restored, hierarchy intact.
        assert (dest / "src" / "a.txt").read_bytes() == b"alpha"
        assert (dest / "src" / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 4

    def test_identical_existing_skipped_silently(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "src").mkdir()
        (dest / "src" / "a.txt").write_bytes(b"alpha")
        calls = []
        records = [env["files"].get_by_path(sid, "src/a.txt")]
        report = env["restore"].restore_files(
            records, dest, collision_callback=lambda *a: calls.append(a) or CollisionDecision(CollisionAction.OVERWRITE),
        )
        assert report.skipped == 1 and calls == []

    def test_collision_overwrite_and_keep_both(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "src").mkdir()
        (dest / "src" / "a.txt").write_bytes(b"different-content!")
        records = [env["files"].get_by_path(sid, "src/a.txt")]
        report = env["restore"].restore_files(
            records, dest,
            collision_callback=lambda *a: CollisionDecision(CollisionAction.KEEP_BOTH),
        )
        assert report.restored == 1
        assert (dest / "src" / "a (2).txt").read_bytes() == b"alpha"
        assert (dest / "src" / "a.txt").read_bytes() == b"different-content!"

    def test_collision_cancel(self, env):
        sid = self._backed_up(env)
        dest = env["tmp"] / "out"
        dest.mkdir()
        (dest / "src").mkdir()
        (dest / "src" / "a.txt").write_bytes(b"x")
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
        rec = env["files"].get_by_path(sid, "src/a.txt")
        env["file_gw"]._blobs[rec.telegram_msg_id] = b"tampered!"
        dest = env["tmp"] / "out"
        report = env["restore"].restore_files([rec], dest)
        assert len(report.failed) == 1
        assert not (dest / "src" / "a.txt").exists()

    def test_unbacked_file_fails(self, env):
        sid, _ = _link_storage(env)
        env["backup"].plan(sid, env["src"])  # indexed but not uploaded
        rec = env["files"].get_by_path(sid, "src/a.txt")
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


class TestTransientTelegramErrors:
    """Telegram rate limits and unexpected replies must be retried, not fatal.

    Regression: the retry path requeued a transfer from its in-flight state,
    which the state machine rejected, so the user saw "Illegal transition
    uploading -> uploading" instead of a completed backup.
    """

    def _rate_limit(self):
        from teloude.infrastructure.telegram.exceptions import RateLimitExceeded

        return RateLimitExceeded(
            "Telegram temporarily limited this operation", retry_after=30
        )

    def test_rate_limit_during_upload_is_retried(self, env):
        sid, _ = _link_storage(env)
        env["file_gw"].fail_next_upload_with = self._rate_limit()
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.uploaded == 2 and report.failed == [], report

    def test_rate_limit_during_download_is_retried(self, env):
        sid, _ = _link_storage(env)
        env["backup"].run(env["backup"].plan(sid, env["src"]))
        rows = [f for f in env["files"].list_by_storage(sid) if f.is_backed_up]
        env["file_gw"].fail_next_download_with = self._rate_limit()
        result = env["restore"].restore_files(rows, env["tmp"] / "out")
        assert result.restored == len(rows) and result.failed == [], result

    def test_persistent_rate_limit_fails_with_the_real_message(self, env):
        from teloude.infrastructure.telegram.exceptions import RateLimitExceeded

        class AlwaysLimited(FakeFileGateway):
            def upload(self, *args, **kwargs):
                raise RateLimitExceeded("Telegram temporarily limited this operation")

        env["backup"]._gateway = AlwaysLimited()
        sid, _ = _link_storage(env)
        report = env["backup"].run(env["backup"].plan(sid, env["src"]))
        assert report.uploaded == 0 and len(report.failed) == 2
        for _path, message in report.failed:
            assert "temporarily limited" in message.lower()
            assert "illegal transition" not in message.lower()
        # the row is failed (not stuck mid-flight) so the Retry button applies
        rows = env["registry"]._repo.list_recent(50)
        assert {r.status for r in rows} == {TransferState.FAILED.value}

    def test_transfer_state_machine_allows_a_retry_from_in_flight_states(self):
        from teloude.core.transfers import TRANSITIONS

        for state in (TransferState.UPLOADING, TransferState.DOWNLOADING):
            assert TransferState.QUEUED in TRANSITIONS[state], state
            assert TransferState.FAILED in TRANSITIONS[state], state


class TestTransferHistoryGrowth:
    def test_prune_history_keeps_active_rows_and_the_newest_finished(self, env):
        repository = env["registry"]._repo
        sid, _ = _link_storage(env)
        for index in range(120):
            tid = repository.create_or_reset("upload", sid, None, 1, f"/f{index}")
            repository.set_status(tid, "completed" if index % 2 else "failed")
        active = repository.create_or_reset("upload", sid, None, 1, "/active.bin")

        removed = repository.prune_history(keep=20)
        assert removed == 100
        remaining = repository.list_recent(500)
        assert len(remaining) == 21
        assert any(t.id == active for t in remaining)
        assert [t.local_path for t in remaining if t.status == "queued"] == ["/active.bin"]

    def test_prune_history_is_a_noop_when_under_the_cap(self, env):
        repository = env["registry"]._repo
        sid, _ = _link_storage(env)
        repository.create_or_reset("upload", sid, None, 1, "/one.bin")
        assert repository.prune_history(keep=200) == 0
