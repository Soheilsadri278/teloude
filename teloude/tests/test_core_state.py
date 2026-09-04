# teloude/tests/test_core_state.py
"""Transfer state machine, speed limiter, and topic mapping tests."""
import pytest

from teloude.config import AppConfig
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import FileRepository, StorageRepository, TransferRepository
from teloude.core.transfers import TransferRegistry, TransferState, TransferStateError
from teloude.core.speed_limiter import SpeedLimiter, limit_from_mbps
from teloude.core.topics import parse_topic_name, topic_chain, topic_name_for


@pytest.fixture()
def repos(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "t.db")))
    assert manager.initialize()
    yield StorageRepository(manager), FileRepository(manager), TransferRepository(manager)
    close_db_connection(manager)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


class TestSpeedLimiter:
    def test_unlimited_never_sleeps(self):
        clock = FakeClock()
        limiter = SpeedLimiter(None, clock=clock, sleeper=clock.sleep)
        limiter.consume(10 ** 9)
        assert clock.slept == 0.0

    def test_throttles_to_rate(self):
        clock = FakeClock()
        limiter = SpeedLimiter(1000, clock=clock, sleeper=clock.sleep)
        limiter.consume(1000)   # burst covered by initial allowance
        assert clock.slept == 0.0
        limiter.consume(1000)   # no time passed -> must wait ~1s
        assert clock.slept == pytest.approx(1.0)

    def test_allowance_refills_over_time(self):
        clock = FakeClock()
        limiter = SpeedLimiter(1000, clock=clock, sleeper=clock.sleep)
        limiter.consume(1000)
        clock.now += 2.0
        limiter.consume(1000)
        assert clock.slept == 0.0

    def test_rejects_non_positive(self):
        with pytest.raises(ValueError):
            SpeedLimiter(0)
        with pytest.raises(ValueError):
            limit_from_mbps(-1)
        assert limit_from_mbps(None) is None
        assert limit_from_mbps(5) == 5 * 1024 * 1024


class TestTransferRegistry:
    _counter = 0

    def _upload(self, repos):
        type(self)._counter += 1
        n = type(self)._counter
        storages, files, transfers = repos
        sid = storages.create(f"S{n}")
        fid = files.upsert(sid, None, f"/{n}.bin", f"{n}.bin", f"{n}.bin", 100, 1.0, "s", "f")
        return TransferRegistry(transfers), transfers.create_or_reset(
            "upload", sid, fid, 100, f"/{n}.bin"
        )

    def test_pause_resume_cycle(self, repos):
        registry, tid = self._upload(repos)
        registry.transition(tid, TransferState.UPLOADING)
        registry.pause(tid)
        assert registry.transition(tid, TransferState.QUEUED).status == "queued"
        # resume() is the sanctioned path paused -> queued
        registry2, tid2 = self._upload(repos)
        registry2.transition(tid2, TransferState.UPLOADING)
        registry2.pause(tid2)
        rec = registry2.resume(tid2)
        assert rec.status == TransferState.QUEUED

    def test_illegal_transition_rejected(self, repos):
        registry, tid = self._upload(repos)
        with pytest.raises(TransferStateError):
            registry.transition(tid, TransferState.COMPLETED)  # queued -> completed illegal
        with pytest.raises(TransferStateError):
            registry.resume(tid)  # not paused

    def test_fail_retry_resets_progress(self, repos):
        registry, tid = self._upload(repos)
        registry.transition(tid, TransferState.UPLOADING)
        registry.checkpoint(tid, 60)
        registry.fail(tid, "net down")
        rec = registry.retry(tid)
        assert rec.status == TransferState.QUEUED and rec.done_bytes == 0
        assert rec.attempts == 1

    def test_complete_only_from_verifying(self, repos):
        registry, tid = self._upload(repos)
        registry.transition(tid, TransferState.UPLOADING)
        registry.transition(tid, TransferState.VERIFYING)
        rec = registry.transition(tid, TransferState.COMPLETED)
        assert rec.status == TransferState.COMPLETED
        with pytest.raises(TransferStateError):
            registry.transition(tid, TransferState.QUEUED)  # terminal

    def test_waiting_for_network_requeues(self, repos):
        registry, tid = self._upload(repos)
        registry.transition(tid, TransferState.UPLOADING)
        registry.transition(tid, TransferState.WAITING_FOR_NETWORK)
        rec = registry.transition(tid, TransferState.QUEUED)
        assert rec.status == TransferState.QUEUED

    def test_download_lifecycle(self, repos):
        storages, _, transfers = repos
        sid = storages.create("S")
        registry = TransferRegistry(transfers)
        rec = registry.start_download(sid, None, 50, "/tmp/out.bin")
        assert rec.status == TransferState.QUEUED
        registry.transition(rec.id, TransferState.DOWNLOADING)
        registry.transition(rec.id, TransferState.VERIFYING)
        assert registry.transition(rec.id, TransferState.COMPLETED).status == "completed"


class TestTopics:
    def test_root_topic(self):
        assert topic_name_for("Photos", "") == "Photos"
        assert topic_name_for("Photos", ".") == "Photos"

    def test_nested_mapping(self):
        assert topic_name_for("Photos", "2026/Wedding") == "Photos / 2026 / Wedding"

    def test_roundtrip(self):
        for rel in ("", "2026", "2026/Wedding", "a/b/c/d"):
            title = topic_name_for("Photos", rel)
            assert parse_topic_name(title, "Photos") == rel

    def test_foreign_topic_rejected(self):
        assert parse_topic_name("Documents / 2026", "Photos") is None

    def test_chain_order(self):
        chain = topic_chain("Photos", "2026/Wedding")
        assert [r for r, _ in chain] == ["", "2026", "2026/Wedding"]
        assert chain[0][1] == "Photos"

    def test_long_paths_truncated_deterministically(self):
        long_rel = "/".join(f"folder{i}" for i in range(30))
        t1 = topic_name_for("Photos", long_rel)
        t2 = topic_name_for("Photos", long_rel)
        assert t1 == t2 and len(t1) <= 100
        assert t1 != topic_name_for("Photos", long_rel + "x")
