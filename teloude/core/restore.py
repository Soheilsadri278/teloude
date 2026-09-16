# teloude/core/restore.py
"""Restore engine: Telegram documents back to a user-chosen destination.

Safety rules (never violated):
- Existing local files are never overwritten without an explicit decision.
- Restored paths cannot escape the destination (traversal guard).
- A failed integrity check is never reported as success; partial output is removed.
- Downloads resume exactly from persisted checkpoints (server-side offsets).
"""
import logging
import shutil
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Callable, List, Optional

from teloude.core.control import EngineCancelled, EngineControl
from teloude.core.errors import is_network_error, local_failure_message
from teloude.infrastructure.telegram.exceptions import (
    RemoteItemMissingError,
    SessionExpiredError,
)
from teloude.core.speed_limiter import SpeedLimiter
from teloude.core.transfers import TransferRegistry, TransferState
from teloude.infrastructure.repositories import FileRecord, FileRepository
from teloude.infrastructure.telegram.exceptions import ConnectionStateError
from teloude.infrastructure.telegram.files import (
    ITelegramFileGateway,
    UploadCancelled,
    UploadPaused,
)

logger = logging.getLogger("RestoreEngine")


class RestoreError(Exception):
    """Fatal restore setup failure (unknown file, unsafe path, no space...)."""


class CollisionAction(Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    KEEP_BOTH = "keep_both"
    CANCEL = "cancel"


@dataclass
class CollisionDecision:
    action: CollisionAction
    apply_to_all: bool = False


@dataclass
class RestoreReport:
    total: int = 0
    restored: int = 0
    skipped: int = 0
    failed: List[tuple] = field(default_factory=list)  # (relative_path, error)
    cancelled: bool = False


def safe_destination(dest_dir: Path, relative_path: str) -> Path:
    """Joins dest_dir/relative_path, rejecting escapes (.., absolute, drives)."""
    dest_dir = Path(dest_dir)
    candidate = (dest_dir / relative_path)
    try:
        resolved_base = dest_dir.resolve()
        resolved = candidate.resolve() if candidate.exists() else (resolved_base / relative_path)
        resolved.relative_to(resolved_base)
    except ValueError:
        raise RestoreError(f"Unsafe restore path rejected: {relative_path}")
    if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise RestoreError(f"Unsafe restore path rejected: {relative_path}")
    return dest_dir / relative_path


def keep_both_path(target: Path) -> Path:
    """Finds a free sibling name: 'doc (2).pdf', 'doc (3).pdf', ..."""
    stem, suffix = target.stem, target.suffix
    for i in range(2, 10000):
        candidate = target.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RestoreError(f"Cannot find a free name for {target.name}.")


_SESSION_EXPIRED_MESSAGE = (
    "Your Telegram session has ended. Sign in again to continue."
)


def _blocking_file(path: Path, stop: Path) -> Optional[str]:
    """Names the existing file that prevents folders from being created.

    Returns None when every component up to (and including) stop is fine.
    """
    current = path
    while True:
        try:
            if current.exists() and not current.is_dir():
                return current.name
        except OSError:
            return None
        if current == stop or current.parent == current:
            return None
        current = current.parent


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class RestoreManager:
    def __init__(
        self,
        files: FileRepository,
        registry: TransferRegistry,
        file_gateway: ITelegramFileGateway,
        limiter: Optional[SpeedLimiter] = None,
        max_retries: int = 5,
    ):
        self._files = files
        self._registry = registry
        self._gateway = file_gateway
        self._limiter = limiter or SpeedLimiter(None)
        self._max_retries = max_retries
        self._remembered: Optional[CollisionAction] = None

    def set_limiter(self, limiter: SpeedLimiter) -> None:
        """Swaps the rate limiter (applied to subsequently downloaded bytes)."""
        self._limiter = limiter

    def restore_files(
        self,
        records: List[FileRecord],
        dest_dir: Path,
        collision_callback: Optional[Callable[[FileRecord, Path, int, int], CollisionDecision]] = None,
        control: Optional[EngineControl] = None,
        progress: Optional[Callable[[int, int, int, int, str], None]] = None,
    ) -> RestoreReport:
        control = control or EngineControl()
        dest_dir = Path(dest_dir)
        if dest_dir.exists() and not dest_dir.is_dir():
            raise RestoreError(
                f"The restore destination '{dest_dir}' is a file, not a folder."
            )
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RestoreError(
                local_failure_message(exc, f"Could not create '{dest_dir}'")
            ) from exc
        report = RestoreReport(total=len(records))
        self._remembered = None
        total_bytes = sum(r.size for r in records)
        done_bytes = 0
        done_files = 0
        try:
            for i, rec in enumerate(records):
                control.check_cancelled()
                control.wait_if_paused()
                if progress is not None:
                    progress(done_files, len(records), done_bytes, total_bytes, rec.relative_path)
                try:
                    outcome = self._restore_one(
                        rec, dest_dir, control,
                        collision_callback, i + 1, len(records),
                    )
                    if outcome == "cancel":
                        report.cancelled = True
                        return report
                    if outcome == "skipped":
                        report.skipped += 1
                    else:
                        report.restored += 1
                except EngineCancelled:
                    raise
                except SessionExpiredError:
                    report.failed.append((rec.relative_path, _SESSION_EXPIRED_MESSAGE))
                    raise
                except Exception as exc:
                    logger.warning(f"Restore failed for {rec.relative_path}: {exc}")
                    report.failed.append((rec.relative_path, str(exc)))
                done_bytes += rec.size
                done_files += 1
        except EngineCancelled:
            report.cancelled = True
        if progress is not None:
            progress(done_files, len(records), done_bytes, total_bytes, "")
        return report

    def _restore_one(
        self,
        rec: FileRecord,
        dest_dir: Path,
        control: EngineControl,
        collision_callback,
        index: int,
        total: int,
    ) -> str:
        if not rec.is_backed_up or rec.telegram_chat_id is None or rec.telegram_msg_id is None:
            raise RestoreError(f"{rec.relative_path} has no cloud backup to restore.")
        target = safe_destination(dest_dir, rec.relative_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            blocker = _blocking_file(target.parent, dest_dir)
            if blocker is not None:
                raise RestoreError(
                    f"{rec.relative_path}: '{blocker}' is a file, but a folder is "
                    "needed there."
                ) from exc
            raise RestoreError(local_failure_message(
                exc, f"Could not create the folder for {rec.relative_path}"
            )) from exc
        if target.exists():
            action = self._remembered
            if action is None:
                if self._identical(target, rec):
                    return "skipped"  # already restored; no prompt needed
                if collision_callback is None:
                    return "skipped"
                decision = collision_callback(rec, target, index, total)
                action = decision.action
                if decision.apply_to_all and action is not CollisionAction.CANCEL:
                    self._remembered = action
            if action is CollisionAction.CANCEL:
                return "cancel"
            if action is CollisionAction.SKIP:
                return "skipped"
            if action is CollisionAction.KEEP_BOTH:
                target = keep_both_path(target)
            # OVERWRITE proceeds to the same target.
        self._check_space(target, rec.size)
        transfer = self._registry.start_download(
            rec.storage_id, rec.id, rec.size, str(target)
        )
        transfer = self._registry.transition(transfer.id, TransferState.DOWNLOADING)
        attempts = 0
        offset = 0
        while True:
            control.check_cancelled()
            last_done = [offset]

            def on_progress(done: int) -> None:
                delta = done - last_done[0]
                last_done[0] = done
                if delta > 0:
                    self._limiter.consume(delta)
                self._registry.checkpoint(transfer.id, done)

            try:
                doc = self._gateway.resolve_document(rec.telegram_chat_id, rec.telegram_msg_id)
                self._gateway.download(
                    doc, target, offset=offset, progress=on_progress,
                    should_pause=control.should_pause, is_cancelled=control.is_cancelled,
                )
            except UploadPaused:
                self._registry.pause(transfer.id)
                control.wait_if_paused()
                transfer = self._registry.resume(transfer.id)
                transfer = self._registry.transition(transfer.id, TransferState.DOWNLOADING)
                offset = self._checkpoint_of(transfer.id)
                continue
            except UploadCancelled:
                self._registry.cancel(transfer.id)
                raise EngineCancelled("cancelled during download")
            except RemoteItemMissingError as exc:
                # A deleted message stays deleted: fail this file, keep the rest.
                self._registry.fail(transfer.id, str(exc))
                raise RestoreError(str(exc)) from exc
            except SessionExpiredError as exc:
                # An expired session stops the whole run; the services layer
                # turns it into a re-authentication flow.
                self._registry.fail(transfer.id, str(exc))
                raise
            except (OSError, ConnectionStateError) as exc:
                if not is_network_error(exc):
                    message = local_failure_message(exc, rec.relative_path)
                    self._registry.fail(transfer.id, message)
                    raise RestoreError(message) from exc
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, f"network unreachable: {exc}")
                    raise RestoreError(f"Network unreachable during {rec.relative_path}.")
                self._registry.transition(transfer.id, TransferState.WAITING_FOR_NETWORK)
                transfer = self._requeue_download(transfer.id)
                offset = self._checkpoint_of(transfer.id)
                continue
            except Exception as exc:
                attempts += 1
                logger.warning(
                    f"Download attempt {attempts} for {rec.relative_path} failed: {exc}"
                )
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, str(exc))
                    raise
                transfer = self._requeue_download(transfer.id)
                offset = 0
                continue
            break
        # Integrity verification: size + SHA-256 when the index has them.
        self._registry.transition(transfer.id, TransferState.VERIFYING)
        actual_size = target.stat().st_size
        if actual_size != rec.size or (rec.sha256 and file_sha256(target) != rec.sha256):
            try:
                target.unlink()
            except OSError:
                pass
            self._registry.fail(transfer.id, "integrity mismatch")
            raise RestoreError(f"Integrity check failed for {rec.relative_path}.")
        self._registry.transition(transfer.id, TransferState.COMPLETED)
        return "restored"

    @staticmethod
    def _identical(target: Path, rec: FileRecord) -> bool:
        try:
            if target.stat().st_size != rec.size:
                return False
        except OSError:
            return False
        if rec.sha256:
            try:
                return file_sha256(target) == rec.sha256
            except OSError:
                return False
        return False

    @staticmethod
    def _check_space(target: Path, needed: int) -> None:
        try:
            free = shutil.disk_usage(target.parent).free
        except OSError:
            return  # cannot determine; attempt anyway and surface real errors
        if needed > free:
            raise RestoreError(
                f"Not enough disk space for {target.name} "
                f"(needs {needed} bytes, {free} free)."
            )

    def _requeue_download(self, transfer_id: int):
        """Puts a download back in the queue for another attempt.

        Handles every state a retry starts from, including the in-flight
        DOWNLOADING state (rate limits and other non-network failures).
        """
        rec = self._registry.active_record(transfer_id)
        state = TransferState(rec.status) if rec else None
        if state is TransferState.PAUSED:
            self._registry.resume(transfer_id)
        elif state in (TransferState.WAITING_FOR_NETWORK, TransferState.VERIFYING,
                       TransferState.FAILED, TransferState.DOWNLOADING):
            self._registry.transition(transfer_id, TransferState.QUEUED)
        return self._registry.transition(transfer_id, TransferState.DOWNLOADING)

    def _checkpoint_of(self, transfer_id: int) -> int:
        rec = self._registry.active_record(transfer_id)
        return rec.done_bytes if rec else 0
