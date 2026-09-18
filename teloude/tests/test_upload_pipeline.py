# teloude/tests/test_upload_pipeline.py
"""The upload path's speed contract: part size, pipelining, and invariants.

The 2026-09 slowdown: uploads ran one 128 KiB part per round trip (2.2 MB/s at
60 ms RTT, measured) because the gateway awaited every acknowledgement before
dispatching the next part and the configured 512 KiB chunk never reached the
gateway. These tests pin the fix without loosening any guarantee: parts stay
ordered, progress stays an absolute prefix, md5/bytes are identical to the
sequential loop, pause/cancel keep their checkpoints, and failures drain every
in-flight answer before surfacing.
"""
import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest

from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.restore import RestoreManager
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.exceptions import TeloudeTelegramError
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway
from teloude.infrastructure.telegram.files import (
    DEFAULT_UPLOAD_WINDOW,
    MAX_PART_BYTES,
    MIN_PART_BYTES,
    TelethonFileGateway,
    UploadCancelled,
    UploadPaused,
    clamp_part_bytes,
)


class TestPartSizeClamp:
    def test_default_is_the_protocol_maximum(self):
        assert clamp_part_bytes(512) == MAX_PART_BYTES == 512 * 1024

    def test_configured_values_pass_through(self):
        assert clamp_part_bytes(256) == 256 * 1024
        assert clamp_part_bytes(64) == 64 * 1024

    def test_below_minimum_clamps_to_16k(self):
        assert clamp_part_bytes(0) == MIN_PART_BYTES == 16 * 1024
        assert clamp_part_bytes(-10) == MIN_PART_BYTES

    def test_above_maximum_clamps_to_512k(self):
        assert clamp_part_bytes(4096) == MAX_PART_BYTES

    def test_result_is_always_a_multiple_of_4k(self):
        assert clamp_part_bytes(17) % 4096 == 0
        assert clamp_part_bytes(100) % 4096 == 0


class TestConfiguredChunk:
    def test_default_setting_is_512k(self):
        assert AppConfig().get_chunk_bytes() == 512 * 1024

    def test_setting_is_clamped_and_aligned(self):
        assert AppConfig(chunk_size_kb=64).get_chunk_bytes() == 64 * 1024
        assert AppConfig(chunk_size_kb=1).get_chunk_bytes() == 16 * 1024
        assert AppConfig(chunk_size_kb=9999).get_chunk_bytes() == 512 * 1024
        assert AppConfig(chunk_size_kb=17).get_chunk_bytes() % 4096 == 0


class TestManagerPlumbing:
    """The engines pass the configured chunk to the gateway."""

    @pytest.fixture()
    def env(self, tmp_path):
        manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "pipe.db")))
        assert manager.initialize()
        storages = StorageRepository(manager)
        repos = dict(
            storages=storages,
            folders=FolderRepository(manager),
            files=FileRepository(manager),
            registry=TransferRegistry(TransferRepository(manager)),
        )
        repos["file_gw"] = FakeFileGateway()
        repos["storage_gw"] = FakeStorageGateway()
        repos["manager"] = manager
        repos["tmp"] = tmp_path
        yield repos
        close_db_connection(manager)

    def _link_and_backup(self, env, root, part_bytes=None):
        info = env["storage_gw"].create_storage("S")
        sid = env["storages"].create("S")
        env["storages"].set_telegram(sid, info.chat_id, True)
        backup = BackupManager(
            env["storages"], env["folders"], env["files"], env["registry"],
            env["storage_gw"], env["file_gw"], max_retries=1,
            retry_sleeper=lambda _s: None, part_bytes=part_bytes,
        )
        plan = backup.plan(sid, root)
        return backup.run(plan)

    def test_backup_uses_the_configured_part_size(self, env):
        root = env["tmp"] / "root"
        root.mkdir()
        (root / "a.bin").write_bytes(b"x" * 300_000)
        self._link_and_backup(env, root, part_bytes=256 * 1024)
        # 300 KB in 256 KiB parts -> exactly 2 parts, not the fake's 64 KiB.
        assert env["file_gw"].uploaded_parts == [0, 1]

    def test_backup_without_a_setting_keeps_the_gateway_suggestion(self, env):
        root = env["tmp"] / "root"
        root.mkdir()
        (root / "a.bin").write_bytes(b"x" * 300_000)
        self._link_and_backup(env, root, part_bytes=None)
        assert env["file_gw"].suggest_part_size(300_000) == 64 * 1024
        assert len(env["file_gw"].uploaded_parts) == -(-300_000 // (64 * 1024))

    def test_restore_passes_the_configured_chunk(self, env):
        root = env["tmp"] / "root"
        root.mkdir()
        (root / "a.bin").write_bytes(b"y" * 50_000)
        self._link_and_backup(env, root, part_bytes=32 * 1024)
        record = env["files"].get_by_path(
            env["storages"].list_all()[0].id, "root/a.bin"
        )
        seen = {}

        real_download = env["file_gw"].download

        def spy_download(doc, dest, **kwargs):
            seen["chunk"] = kwargs.get("chunk")
            return real_download(doc, dest, **kwargs)

        env["file_gw"].download = spy_download
        restore = RestoreManager(
            env["files"], env["registry"], env["file_gw"],
            max_retries=1, part_bytes=32 * 1024,
        )
        dest = env["tmp"] / "out"
        dest.mkdir()
        restore.restore_files([record], dest)
        assert seen["chunk"] == 32 * 1024
        assert (dest / "root" / "a.bin").read_bytes() == b"y" * 50_000


class ScriptedUpload:
    """Collects SaveFilePart calls (part index -> bytes), can fail on cue."""

    def __init__(self, fail_at=None, latency=0.0, async_mode=False):
        self.saved = {}
        self.fail_at = fail_at
        self.latency = latency
        self.async_mode = async_mode
        self.failures_raised = 0

    async def _serve(self, request):
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail_at is not None and request.file_part == self.fail_at:
            self.failures_raised += 1
            raise ConnectionError("simulated part failure")
        self.saved[request.file_part] = bytes(request.bytes)
        return True

    def __call__(self, request):
        if self.async_mode:
            return self._serve(request)
        if self.latency:
            time.sleep(self.latency)
        if self.fail_at is not None and request.file_part == self.fail_at:
            self.failures_raised += 1
            raise ConnectionError("simulated part failure")
        self.saved[request.file_part] = bytes(request.bytes)
        return True


def _write(temp, size, seed=b"\xa5"):
    path = temp / "up.bin"
    path.write_bytes(seed * size)
    return path


class TestWindowedUploadEquivalence:
    """window=8 must be byte-for-byte identical to the old sequential loop."""

    def test_same_parts_bytes_and_digest(self, tmp_path):
        source = _write(tmp_path, 10_000, bytes(range(256))[:40])
        sequential = ScriptedUpload()
        windowed = ScriptedUpload()
        one = TelethonFileGateway(invoke=sequential).upload(
            source, part_size=1024, window=1
        )
        eight = TelethonFileGateway(invoke=windowed).upload(
            source, part_size=1024, window=8
        )
        assert sequential.saved == windowed.saved
        from dataclasses import replace

        assert replace(one, file_id=eight.file_id) == eight  # parts, md5, size
        assert hashlib.md5(source.read_bytes()).hexdigest() == eight.md5

    def test_default_window_is_pipelined(self):
        assert DEFAULT_UPLOAD_WINDOW == 8

    def test_progress_is_an_ordered_absolute_prefix(self, tmp_path):
        source = _write(tmp_path, 8192)
        seen = []
        TelethonFileGateway(invoke=ScriptedUpload()).upload(
            source, progress=seen.append, part_size=1024, window=8
        )
        assert seen == sorted(seen) and seen[-1] == 8192
        assert all(done % 1024 == 0 for done in seen)

    def test_resume_continues_from_the_checkpoint(self, tmp_path):
        source = _write(tmp_path, 4000)
        target = ScriptedUpload()
        gateway = TelethonFileGateway(invoke=target)
        calls = {"n": 0}

        def pause_after_two():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(UploadPaused) as paused:
            gateway.upload(
                source, part_size=1000, window=8, should_pause=pause_after_two,
            )
        assert paused.value.done_bytes == 2000
        first_id = paused.value.file_id
        assert list(target.saved) == [0, 1]
        gateway.upload(
            source, part_size=1000, window=8, start_part=2, file_id=first_id,
        )
        assert sorted(target.saved) == list(range(4))
        assert b"".join(target.saved[i] for i in range(4)) == source.read_bytes()


class TestWindowedStopConditions:
    def test_cancel_before_any_part(self, tmp_path):
        source = _write(tmp_path, 5000)
        target = ScriptedUpload()
        with pytest.raises(UploadCancelled):
            TelethonFileGateway(invoke=target).upload(
                source, part_size=1000, window=8, is_cancelled=lambda: True,
            )
        assert target.saved == {}

    def test_cancel_mid_window_retires_in_flight_parts(self, tmp_path):
        source = _write(tmp_path, 8000)
        target = ScriptedUpload()
        calls = {"n": 0}

        def cancel_third():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(UploadCancelled):
            TelethonFileGateway(invoke=target).upload(
                source, part_size=1000, window=8, is_cancelled=cancel_third,
            )
        # The two dispatched parts were fully retired before the raise; cancel
        # itself carries no checkpoint (only UploadPaused does).
        assert sorted(target.saved) == [0, 1]
        assert calls["n"] == 3

    def test_synchronous_dispatch_failure_drains_in_flight(self, tmp_path):
        source = _write(tmp_path, 8000)
        target = ScriptedUpload(fail_at=2)
        with pytest.raises(TeloudeTelegramError):
            TelethonFileGateway(invoke=target).upload(
                source, part_size=1000, window=8,
            )
        assert target.failures_raised == 1

    def test_async_failure_surfaces_after_draining(self, tmp_path):
        source = _write(tmp_path, 12_000)
        target = ScriptedUpload(fail_at=2, latency=0.01, async_mode=True)
        with pytest.raises(TeloudeTelegramError):
            TelethonFileGateway(invoke=target).upload(
                source, part_size=1000, window=8,
            )
        assert target.failures_raised == 1


class TestPipeliningIsTheSpeedup:
    """The measurable point of the change: latency hides behind the window."""

    def test_windowed_upload_beats_sequential_under_latency(self, tmp_path):
        source = _write(tmp_path, 6 * 64 * 1024)
        latency = 0.03  # 30 ms per part answer
        # async_mode=True is how the live Telethon client behaves: every invoke
        # is an awaitable served by the loop thread, so answers can overlap.
        # A synchronous double cannot overlap by construction.

        sequential = ScriptedUpload(latency=latency, async_mode=True)
        t0 = time.perf_counter()
        TelethonFileGateway(invoke=sequential).upload(
            source, part_size=64 * 1024, window=1,
        )
        sequential_seconds = time.perf_counter() - t0

        windowed = ScriptedUpload(latency=latency, async_mode=True)
        t0 = time.perf_counter()
        TelethonFileGateway(invoke=windowed).upload(
            source, part_size=64 * 1024, window=8,
        )
        windowed_seconds = time.perf_counter() - t0

        # Six sequential round trips cost at least 6 x 30 ms.
        assert sequential_seconds >= 0.15
        # One window of six parts costs about one round trip.
        assert windowed_seconds < sequential_seconds * 0.75
        assert sequential.saved == windowed.saved

    def test_single_part_files_are_unaffected(self, tmp_path):
        source = _write(tmp_path, 512)
        target = ScriptedUpload()
        uploaded = TelethonFileGateway(invoke=target).upload(source, part_size=1024)
        assert uploaded.parts == 1 and target.saved == {0: source.read_bytes()}


class TestDownloadChunk:
    def test_download_honours_the_configured_chunk(self, tmp_path):
        blob = bytes(range(256)) * 40  # 10240 bytes

        limits = []

        def get_file(request):
            limits.append(request.limit)
            return SimpleNamespace(bytes=blob[request.offset: request.offset + request.limit])

        from teloude.infrastructure.telegram.files import DocumentRef

        gateway = TelethonFileGateway(invoke=ScriptedUpload())  # never used for get
        gateway._invoke = SimpleNamespace()  # guard: downloads go through _call
        class GetOnly:
            def __call__(self, request):
                return get_file(request)

        gateway = TelethonFileGateway(invoke=GetOnly())
        doc = DocumentRef(doc_id=1, access_hash=1, file_reference=b"r",
                          size=len(blob), file_name="n", mime="m")
        dest = tmp_path / "out.bin"
        written = gateway.download(doc, dest, chunk=32 * 1024)
        assert written == len(blob) and dest.read_bytes() == blob
        assert limits and all(l == 32 * 1024 for l in limits)

    def test_download_default_is_unchanged(self, tmp_path):
        blob = b"1234"

        limits = []

        class GetOnly:
            def __call__(self, request):
                limits.append(request.limit)
                return SimpleNamespace(bytes=blob[request.offset: request.offset + request.limit])

        from teloude.infrastructure.telegram.files import DocumentRef

        gateway = TelethonFileGateway(invoke=GetOnly())
        doc = DocumentRef(doc_id=1, access_hash=1, file_reference=b"r",
                          size=4, file_name="n", mime="m")
        dest = tmp_path / "out.bin"
        gateway.download(doc, dest)
        # The loop's final empty answer (end of blob) uses the same limit.
        assert set(limits) == {512 * 1024}
