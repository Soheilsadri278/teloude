# teloude/core/scanner.py
"""Filesystem scanner: read-only discovery of files for backup.

Never modifies, moves, renames, or deletes local files. Files are opened
read-only for hashing. Inaccessible entries are reported via on_error and
skipped; a missing root raises ScanError.
"""
from dataclasses import dataclass
from hashlib import sha256 as _sha256
from pathlib import Path
from typing import Callable, Iterator, List, Optional
import logging
import os

logger = logging.getLogger("FileScanner")

HASH_CHUNK = 1024 * 1024  # 1 MiB streaming reads; never load whole files into RAM.


class ScanError(Exception):
    """Raised when a scan cannot start (missing root, not a directory)."""


@dataclass
class ScannedFile:
    path: Path          # absolute local path
    relative: str       # posix-style path relative to the scan root
    name: str           # file name only
    size: int           # bytes
    mtime_ns: int       # modification time (ns) for change detection
    fingerprint: str    # cheap identity: "<size>:<mtime_ns>"
    sha256: Optional[str] = None  # filled on demand by hash_scanned()


def compute_fingerprint(size: int, mtime_ns: int) -> str:
    return f"{size}:{mtime_ns}"


def sha256_of(
    path: Path,
    chunk_size: int = HASH_CHUNK,
    progress: Optional[Callable[[int], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> str:
    """Streams a file through SHA-256 without loading it into RAM."""
    digest = _sha256()
    with open(path, "rb") as fh:  # read-only; source file untouched
        while True:
            if is_cancelled is not None and is_cancelled():
                raise InterruptedError(f"Hashing cancelled: {path}")
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return digest.hexdigest()


def hash_scanned(
    scanned: ScannedFile,
    progress: Optional[Callable[[int], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> ScannedFile:
    scanned.sha256 = sha256_of(scanned.path, progress=progress, is_cancelled=is_cancelled)
    return scanned


def iter_files(
    root: Path,
    on_error: Optional[Callable[[Path, Exception], None]] = None,
) -> Iterator[ScannedFile]:
    """Yields ScannedFile rows (without hashes) for every file under root.

    Breadth: follows no symlinks (avoids cycles), skips sockets/FIFOs/devices,
    reports OSErrors via on_error and continues.
    """
    root = Path(root)
    if not root.exists():
        raise ScanError(f"Scan root does not exist: {root}")
    if not root.is_dir() or root.is_symlink():
        raise ScanError(f"Scan root is not a directory: {root}")

    def report(path: Path, exc: Exception) -> None:
        logger.warning(f"Skipping {path}: {exc}")
        if on_error is not None:
            on_error(path, exc)

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            report(current, exc)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        report(Path(entry.path), exc)
                        continue
                    full = Path(entry.path)
                    try:
                        relative = full.relative_to(root).as_posix()
                    except ValueError as exc:  # pragma: no cover - defensive
                        report(full, exc)
                        continue
                    yield ScannedFile(
                        path=full,
                        relative=relative,
                        name=full.name,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        fingerprint=compute_fingerprint(stat.st_size, stat.st_mtime_ns),
                    )
                # else: sockets, FIFOs, devices -> silently skipped
            except OSError as exc:
                report(Path(entry.path), exc)


def scan_directory(
    root: Path,
    on_error: Optional[Callable[[Path, Exception], None]] = None,
) -> List[ScannedFile]:
    """Eager scan returning all files sorted by relative path (deterministic)."""
    return sorted(iter_files(root, on_error=on_error), key=lambda s: s.relative)
