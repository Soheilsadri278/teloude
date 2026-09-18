# teloude/tests/test_search_preview.py
"""Search service and preview engine tests."""
import io
import os
import subprocess

import pytest

from teloude.config import AppConfig
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import FileRepository, StorageRepository
from teloude.core.search import search_files


@pytest.fixture()
def indexed(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "s.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    files = FileRepository(manager)
    photos = storages.create("Photos")
    docs = storages.create("Documents")
    files.upsert(photos, None, "/p/wedding1.CR3", "wedding1.CR3", "wedding1.CR3",
                 100, 1.0, "s1", "f1")
    files.upsert(photos, None, "/p/wedding2.CR3", "2026/wedding2.CR3", "wedding2.CR3",
                 100, 1.0, "s2", "f2")
    files.upsert(docs, None, "/d/report.pdf", "report.pdf", "report.pdf",
                 50, 1.0, "s3", "f3")
    fid = files.upsert(photos, None, "/p/done.jpg", "done.jpg", "done.jpg",
                       10, 1.0, "s4", "f4")
    files.mark_backed_up(fid, -1001, 9)
    yield manager
    close_db_connection(manager)


class TestSearch:
    def test_finds_across_storages(self, indexed):
        results = search_files(indexed, "wedding")
        assert {r.file_name for r in results} == {"wedding1.CR3", "wedding2.CR3"}
        assert {r.storage_name for r in results} == {"Photos"}

    def test_empty_query_returns_nothing(self, indexed):
        assert search_files(indexed, "   ") == []

    def test_no_match(self, indexed):
        assert search_files(indexed, "zzz-no-such-file") == []

    def test_storage_filter(self, indexed):
        assert search_files(indexed, "report", storage_id=9999) == []
        results = search_files(indexed, "report")
        assert len(results) == 1 and results[0].storage_name == "Documents"

    def test_backed_only(self, indexed):
        results = search_files(indexed, "jpg", backed_only=True)
        assert len(results) == 1 and results[0].is_backed_up is True
        assert search_files(indexed, "CR3", backed_only=True) == []

    def test_special_characters_safe(self, indexed):
        # Must not raise (FTS operators / LIKE wildcards in user input).
        assert search_files(indexed, 'wedding" OR "1') is not None
        assert search_files(indexed, "100%_x") == []

    def test_like_fallback_path(self, indexed, tmp_path):
        indexed.execute_query("DROP TABLE files_fts")
        assert indexed.has_fts() is False
        results = search_files(indexed, "wedding")
        assert {r.file_name for r in results} == {"wedding1.CR3", "wedding2.CR3"}


class TestPreview:
    def test_image_thumbnail(self, tmp_path):
        from PIL import Image
        img_path = tmp_path / "photo.jpg"
        Image.new("RGB", (800, 600), "red").save(img_path, "JPEG")
        from teloude.core.preview import generate_preview
        result = generate_preview(img_path)
        assert result.kind == "image" and result.thumbnail_png is not None
        assert result.width <= 384 and result.height <= 384
        with Image.open(io.BytesIO(result.thumbnail_png)) as thumb:
            assert thumb.format == "PNG"

    def test_corrupt_image_never_raises(self, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not a jpeg at all" * 100)
        from teloude.core.preview import generate_preview
        result = generate_preview(bad)
        assert result.kind == "none" and result.thumbnail_png is None

    def test_unsupported_type(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("hello")
        from teloude.core.preview import generate_preview
        result = generate_preview(txt)
        assert result.kind == "none"

    def test_missing_file_raises(self, tmp_path):
        from teloude.core.preview import generate_preview
        with pytest.raises(FileNotFoundError):
            generate_preview(tmp_path / "ghost.png")

    def test_raw_embedded_jpeg(self, tmp_path):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), "blue").save(buf, "JPEG")
        raw = tmp_path / "shot.CR3"
        raw.write_bytes(b"CR3FAKEHEADER" * 500 + buf.getvalue() + b"TRAILER")
        from teloude.core.preview import generate_preview
        result = generate_preview(raw)
        assert result.thumbnail_png is not None
        assert result.source == "raw-embedded"

    def test_raw_without_preview_falls_back(self, tmp_path):
        raw = tmp_path / "shot.NEF"
        raw.write_bytes(os.urandom(4096))
        from teloude.core.preview import generate_preview
        result = generate_preview(raw)
        assert result.thumbnail_png is None and result.kind == "image"

    def test_cache_roundtrip(self, tmp_path):
        from PIL import Image
        img_path = tmp_path / "c.png"
        Image.new("RGB", (100, 100), "green").save(img_path)
        from teloude.core.preview import generate_preview
        cache = tmp_path / "cache"
        first = generate_preview(img_path, cache_dir=cache)
        second = generate_preview(img_path, cache_dir=cache)
        assert first.thumbnail_png == second.thumbnail_png
        assert second.source == "cache"
        assert len(list(cache.glob("*.png"))) == 1

    def _real_clip(self, tmp_path):
        """A real encoded video, written by the declared imageio-ffmpeg binary.

        The fixture used to be built with `imageio` and `numpy`, which no
        dependency file declares: it passed only where something else had already
        installed them, and the Windows CI runner failed the test with
        `ModuleNotFoundError: No module named 'imageio'`. Video in this project is
        produced by the ffmpeg binary that `imageio-ffmpeg` ships (see
        `teloude/core/preview.py`), so the fixture is built with that binary and
        the test needs nothing beyond the declared dependencies.
        """
        ffmpeg = pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
        video = tmp_path / "clip.mp4"
        attempts = []
        # libx264 first - what real-world clips use, and what the old fixture
        # produced; mpeg4 is the encoder every ffmpeg build carries, so this also
        # works if a build were ever shipped without libx264.
        for encoder in ("libx264", "mpeg4"):
            done = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc=size=64x64:rate=8:duration=3",
                 "-pix_fmt", "yuv420p", "-c:v", encoder, str(video)],
                capture_output=True, timeout=60,
            )
            detail = done.stderr.decode("utf-8", "replace").strip()
            attempts.append(f"{encoder}: rc={done.returncode} {detail}")
            if done.returncode == 0 and video.exists() and video.stat().st_size > 0:
                return video
        raise AssertionError(
            "the bundled ffmpeg could not write a test video: " + "; ".join(attempts)
        )

    def test_video_thumbnail_real_frame(self, tmp_path):
        vid = self._real_clip(tmp_path)
        from teloude.core.preview import generate_preview
        result = generate_preview(vid)
        assert result.kind == "video"
        assert result.thumbnail_png is not None
        assert result.thumbnail_png[:8] == b"\x89PNG\r\n\x1a\n", (
            "the thumbnail must be a real PNG, not an empty buffer"
        )
        assert result.source == "video-frame"

    def test_broken_video_falls_back(self, tmp_path):
        pytest.importorskip("imageio_ffmpeg")
        vid = tmp_path / "broken.mp4"
        vid.write_bytes(os.urandom(2048))
        from teloude.core.preview import generate_preview
        result = generate_preview(vid)
        assert result.kind == "video" and result.thumbnail_png is None


def os_urandom_compat(n):
    import os
    return os.urandom(n)


def test_preview_cache_is_bounded_by_count_and_size(tmp_path, monkeypatch):
    """A long-lived install must not grow the preview cache without bound."""
    from teloude.core import preview as preview_module

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(preview_module, "CACHE_LIMIT", 5)
    monkeypatch.setattr(preview_module, "CACHE_MAX_BYTES", 3000)

    for index in range(12):
        blob = cache / f"entry{index:02d}.png"
        blob.write_bytes(b"x" * 1000)  # 1 KB each
        os.utime(blob, (1_700_000_000 + index, 1_700_000_000 + index))

    preview_module._cache_prune(cache)

    kept = sorted(cache.glob("*.png"))
    total = sum(path.stat().st_size for path in kept)
    assert len(kept) <= 5, [p.name for p in kept]
    assert total <= 3000, total
    # the newest entries survive, the oldest are dropped
    assert "entry11.png" in {p.name for p in kept}
    assert "entry00.png" not in {p.name for p in kept}


def test_preview_cache_prune_survives_a_missing_directory(tmp_path):
    from teloude.core.preview import _cache_prune

    _cache_prune(tmp_path / "not-created-yet")  # must not raise
