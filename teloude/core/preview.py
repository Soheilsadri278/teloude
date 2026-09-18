# teloude/core/preview.py
"""File previews: images, RAW embedded JPEGs, and video thumbnails.

Preview failures NEVER fail a backup: every path returns a PreviewResult
(including kind='none' with an explanatory detail) instead of raising for
corrupt/unsupported files. Only truly invalid arguments (missing file)
raise. Generation is synchronous; callers offload to worker threads so the
UI never blocks.
"""
import io
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps
    _PILLOW = True
except ImportError:  # pragma: no cover
    _PILLOW = False

try:
    import imageio_ffmpeg
    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover - optional dependency
    _FFMPEG_EXE = None

logger = logging.getLogger("Preview")

IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
RAW_EXTS = frozenset(
    {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".rw2", ".orf", ".raf", ".pef", ".raw"}
)
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"})
MAX_THUMB = (384, 384)
# Preview cache caps: both the file count and the total size, so a long-lived
# install cannot grow the cache without bound regardless of thumbnail sizes.
CACHE_LIMIT = 200
CACHE_MAX_BYTES = 50 * 1024 * 1024


@dataclass
class PreviewResult:
    kind: str                      # 'image' | 'video' | 'none'
    thumbnail_png: Optional[bytes] # PNG bytes (thumbnail) or None
    width: int = 0
    height: int = 0
    detail: str = ""               # human-readable fallback/explanation
    source: str = ""               # 'image' | 'raw-embedded' | 'video-frame' | 'cache' | 'fallback'


def _thumb_png(img: "Image.Image") -> tuple:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail(MAX_THUMB)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.width, img.height


def _preview_image(path: Path) -> PreviewResult:
    with Image.open(path) as img:
        img.load()
        png, w, h = _thumb_png(img)
    return PreviewResult("image", png, w, h, f"{w}x{h} {path.suffix.upper().lstrip('.')}", "image")


def _extract_embedded_jpeg(data: bytes) -> Optional[bytes]:
    """Finds the largest JPEG (SOI...EOI) blob inside a RAW container."""
    best: Optional[bytes] = None
    start = 0
    while True:
        soi = data.find(b"\xff\xd8\xff", start)
        if soi < 0:
            break
        eoi = data.find(b"\xff\xd9", soi + 3)
        if eoi < 0:
            break
        blob = data[soi: eoi + 2]
        if best is None or len(blob) > len(best):
            best = blob
        start = eoi + 2
    return best if best and len(best) > 1024 else None


def _preview_raw(path: Path) -> PreviewResult:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(min(size, 64 * 1024 * 1024))  # 64MB cap; embedded JPEG is near the front
    blob = _extract_embedded_jpeg(head)
    if blob is None:
        return PreviewResult(
            "image", None, 0, 0,
            f"RAW {path.suffix.upper().lstrip('.')} ({size // 1024} KB) - no embedded preview",
            "fallback",
        )
    try:
        with Image.open(io.BytesIO(blob)) as img:
            img.load()
            png, w, h = _thumb_png(img)
        return PreviewResult("image", png, w, h, f"{w}x{h} embedded JPEG", "raw-embedded")
    except Exception as exc:
        logger.debug(f"Embedded JPEG decode failed for {path}: {exc}")
        return PreviewResult("image", None, 0, 0, "RAW preview unavailable", "fallback")


def _preview_video(path: Path) -> PreviewResult:
    detail = f"Video {path.suffix.upper().lstrip('.')}"
    if _FFMPEG_EXE is None:
        return PreviewResult("video", None, 0, 0, detail + " - thumbnail engine missing", "fallback")
    # Try a 1s seek first (fast, representative), then the first frame
    # (short clips may have nothing at the seek position).
    attempts = (["-ss", "1"], [])
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            frame = tmp.name
        try:
            for seek in attempts:
                proc = subprocess.run(
                    [_FFMPEG_EXE, "-y", "-v", "error", *seek, "-i", str(path),
                     "-vframes", "1", "-vf", "scale=384:-1", frame],
                    capture_output=True, timeout=60,
                )
                if (proc.returncode == 0 and os.path.exists(frame)
                        and os.path.getsize(frame) > 0):
                    with Image.open(frame) as img:
                        img.load()
                        png, w, h = _thumb_png(img)
                    return PreviewResult("video", png, w, h, f"{w}x{h} frame", "video-frame")
        finally:
            try:
                os.unlink(frame)
            except OSError:
                pass
    except Exception as exc:
        logger.debug(f"Video thumbnail failed for {path}: {exc}")
    return PreviewResult("video", None, 0, 0, detail + " - no seekable frame", "fallback")


def _cache_key(path: Path) -> str:
    try:
        stat = path.stat()
        token = f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogateescape")
    except OSError:
        token = str(path).encode("utf-8", "surrogateescape")
    return sha256(token).hexdigest() + ".png"


def _cache_prune(cache_dir: Path) -> None:
    """Drops the oldest thumbnails until count and total size are within caps."""
    try:
        entries = [(p, p.stat()) for p in cache_dir.glob("*.png")]
    except OSError:
        return
    entries.sort(key=lambda pair: pair[1].st_mtime, reverse=True)  # newest first
    kept_bytes = 0
    kept_count = 0
    for path, stat in entries:
        fits = kept_count < CACHE_LIMIT and kept_bytes + stat.st_size <= CACHE_MAX_BYTES
        if fits:
            kept_count += 1
            kept_bytes += stat.st_size
            continue
        try:
            path.unlink()
        except OSError:
            pass


def generate_preview(path: Path, cache_dir: Optional[Path] = None) -> PreviewResult:
    """Builds (and optionally caches) a preview. Never raises for bad content."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    key = _cache_key(path)
    if cache_dir is not None:
        cached = cache_dir / key
        if cached.is_file():
            try:
                data = cached.read_bytes()
                with Image.open(io.BytesIO(data)) as img:
                    w, h = img.width, img.height
                return PreviewResult("image", data, w, h, "cached preview", "cache")
            except Exception:
                pass  # corrupt cache entry -> regenerate
    ext = path.suffix.lower()
    try:
        if not _PILLOW:
            return PreviewResult("none", None, 0, 0, "Preview engine unavailable", "fallback")
        if ext in IMAGE_EXTS:
            result = _preview_image(path)
        elif ext in RAW_EXTS:
            result = _preview_raw(path)
        elif ext in VIDEO_EXTS:
            result = _preview_video(path)
        else:
            result = PreviewResult("none", None, 0, 0, f"No preview for {ext or 'extensionless'} files", "fallback")
    except Exception as exc:
        logger.debug(f"Preview failed for {path}: {exc}")
        result = PreviewResult("none", None, 0, 0, "Preview unavailable", "fallback")
    if cache_dir is not None and result.thumbnail_png:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / key).write_bytes(result.thumbnail_png)
            _cache_prune(cache_dir)
        except OSError as exc:
            logger.debug(f"Preview cache write failed: {exc}")
    return result
