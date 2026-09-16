# teloude/core/backup.py
"""Backup engine: scan -> duplicates -> topics -> chunked upload -> verify.

One logical file at a time; internal chunking handled by the file gateway.
Source files are only ever read. Every state change persists through
TransferRegistry, so a restart resumes from checkpoints (same-session part
continuation where possible, safe full-file restart otherwise).
"""
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from teloude.core.control import EngineCancelled, EngineControl
from teloude.core.duplicates import (
    DuplicateAction,
    DuplicateMatch,
    DuplicateResolver,
    find_duplicates,
)
from teloude.core.scanner import ScannedFile, hash_scanned, scan_directory
from teloude.core.speed_limiter import SpeedLimiter
from teloude.core.topics import topic_chain
from teloude.core.transfers import TransferRegistry, TransferState
from teloude.infrastructure.database import utcnow
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
)
from teloude.infrastructure.telegram.exceptions import ConnectionStateError
from teloude.infrastructure.telegram.files import (
    ITelegramFileGateway,
    UploadCancelled,
    UploadPaused,
)
from teloude.infrastructure.telegram.storage import ITelegramStorage

logger = logging.getLogger("BackupEngine")


class BackupError(Exception):
    """Fatal backup setup failure (unknown storage, missing root, ...)."""


@dataclass
class BackupPlan:
    storage_id: int
    storage_name: str
    root: Path
    files: List[ScannedFile] = field(default_factory=list)
    duplicates: List[DuplicateMatch] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class BackupReport:
    total: int = 0
    uploaded: int = 0
    skipped_duplicates: int = 0
    failed: List[tuple] = field(default_factory=list)  # (relative_path, error)
    cancelled: bool = False


class BackupManager:
    def __init__(
        self,
        storages: StorageRepository,
        folders: FolderRepository,
        files: FileRepository,
        registry: TransferRegistry,
        storage_gateway: ITelegramStorage,
        file_gateway: ITelegramFileGateway,
        limiter: Optional[SpeedLimiter] = None,
        max_retries: int = 5,
        retry_sleeper: Callable[[float], None] = time.sleep,
    ):
        self._storages = storages
        self._folders = folders
        self._files = files
        self._registry = registry
        self._storage = storage_gateway
        self._gateway = file_gateway
        self._limiter = limiter or SpeedLimiter(None)
        self._max_retries = max_retries
        self._sleeper = retry_sleeper

    def set_limiter(self, limiter: SpeedLimiter) -> None:
        """Swaps the rate limiter (applied to subsequently uploaded bytes)."""
        self._limiter = limiter

    # -- planning ------------------------------------------------------
    def plan(
        self,
        storage_id: int,
        root: Path,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> BackupPlan:
        storage = self._storages.get(storage_id)
        if storage is None:
            raise BackupError(f"Unknown storage id: {storage_id}")
        root = Path(root)
        scanned = scan_directory(root)
        total = len(scanned)
        for i, item in enumerate(scanned):
            hash_scanned(item)
            self._files.upsert(
                storage_id, None, str(item.path), item.relative, item.name,
                item.size, item.mtime_ns / 1e9, item.sha256, item.fingerprint,
            )
            if progress is not None:
                progress(i + 1, total)

        def lookup(sha: str) -> List[Dict[str, str]]:
            found = []
            for rec in self._files.find_by_sha(sha):
                owner = self._storages.get(rec.storage_id)
                found.append({
                    "storage": owner.name if owner else "?",
                    "relative_path": rec.relative_path,
                })
            return found

        # A file is not its own duplicate: exclude matches pointing at itself.
        matches = [
            m for m in find_duplicates(scanned, lookup)
            if not (
                self._files.get_by_path(storage_id, m.scanned.relative) is not None
                and m.existing_storage == storage.name
                and m.existing_path == m.scanned.relative
            )
        ]
        return BackupPlan(
            storage_id=storage_id, storage_name=storage.name,
            root=root, files=scanned, duplicates=matches,
        )

    # -- execution -----------------------------------------------------
    def run(
        self,
        plan: BackupPlan,
        resolver: Optional[DuplicateResolver] = None,
        control: Optional[EngineControl] = None,
        progress: Optional[Callable[[int, int, int, int, str], None]] = None,
    ) -> BackupReport:
        """Runs the plan. progress(done_files, total_files, done_bytes, total_bytes, current)."""
        control = control or EngineControl()
        resolver = resolver or DuplicateResolver(policy=DuplicateResolver.SKIP_ALL)
        report = BackupReport(total=len(plan.files))
        storage = self._storages.get(plan.storage_id)
        if storage is None or storage.telegram_chat_id is None:
            raise BackupError("Storage is not linked to a Telegram group.")
        chat_id = storage.telegram_chat_id

        skip_paths = set()
        for i, match in enumerate(plan.duplicates):
            action = resolver.decide(match, i + 1, len(plan.duplicates))
            if action is DuplicateAction.CANCEL:
                report.cancelled = True
                return report
            if action is DuplicateAction.SKIP:
                skip_paths.add(match.scanned.relative)
                report.skipped_duplicates += 1

        pending = [f for f in plan.files if f.relative not in skip_paths]
        total_bytes = sum(f.size for f in pending)
        done_bytes = 0
        done_files = 0
        topic_cache: Dict[str, int] = {}
        try:
            for item in pending:
                control.check_cancelled()
                control.wait_if_paused()
                if progress is not None:
                    progress(done_files, len(pending), done_bytes, total_bytes, item.relative)
                folder_id, topic_id = self._ensure_location(
                    plan.storage_id, plan.storage_name, chat_id, item, topic_cache
                )
                try:
                    self._backup_one(plan.storage_id, chat_id, topic_id, folder_id, item, control)
                    report.uploaded += 1
                except EngineCancelled:
                    raise
                except Exception as exc:
                    logger.warning(f"Backup failed for {item.relative}: {exc}")
                    report.failed.append((item.relative, str(exc)))
                done_bytes += item.size
                done_files += 1
        except EngineCancelled:
            report.cancelled = True
        finally:
            self._refresh_storage_stats(plan.storage_id)
        if progress is not None:
            progress(done_files, len(pending), done_bytes, total_bytes, "")
        return report

    # -- crash recovery --------------------------------------------------
    def recover_pending(self) -> int:
        """Requeues interrupted transfers whose source is intact; fails the rest."""
        requeued = 0
        for transfer in self._registry.active():
            if transfer.kind != "upload" or transfer.file_id is None:
                continue
            rec = self._files.get(transfer.file_id)
            if rec is None or rec.is_backed_up:
                # Crash survivors sit in arbitrary active states (a user may even
                # have paused right before the process died), so recovery must
                # not enforce the normal transition table here.
                self._registry.set_status_quiet(
                    transfer.id, TransferState.FAILED, "stale transfer row"
                )
                continue
            try:
                stat = Path(rec.local_path).stat()
                intact = (
                    stat.st_size == rec.size
                    and f"{stat.st_size}:{stat.st_mtime_ns}" == (rec.fingerprint or "")
                )
            except OSError:
                intact = False
            if intact:
                self._registry.set_status_quiet(transfer.id, TransferState.QUEUED)
                requeued += 1
            else:
                self._registry.set_status_quiet(
                    transfer.id, TransferState.FAILED,
                    "source file changed or vanished",
                )
        return requeued

    # -- internals -------------------------------------------------------
    def _ensure_location(
        self,
        storage_id: int,
        storage_name: str,
        chat_id: int,
        item: ScannedFile,
        topic_cache: Dict[str, int],
    ) -> tuple:
        parent = str(Path(item.relative).parent)
        relative_dir = "" if parent == "." else parent.replace("\\", "/")
        self._folders.ensure(
            storage_id, relative_dir, Path(relative_dir).name if relative_dir else storage_name
        )
        folder = self._folders.get_by_path(storage_id, relative_dir)
        assert folder is not None
        if relative_dir in topic_cache:
            return folder.id, topic_cache[relative_dir]
        if folder.telegram_topic_id is None:
            for rel_dir, title in topic_chain(storage_name, relative_dir):
                if rel_dir in topic_cache:
                    continue
                topic = self._storage.ensure_topic(chat_id, title, is_root=(rel_dir == ""))
                topic_cache[rel_dir] = topic.topic_id
                target_id = self._folders.ensure(
                    storage_id, rel_dir,
                    Path(rel_dir).name if rel_dir else storage_name,
                )
                self._folders.set_topic(target_id, topic.topic_id, topic.title)
            folder = self._folders.get_by_path(storage_id, relative_dir)
            assert folder is not None and folder.telegram_topic_id is not None
        topic_cache[relative_dir] = folder.telegram_topic_id
        return folder.id, folder.telegram_topic_id

    def _backup_one(
        self,
        storage_id: int,
        chat_id: int,
        topic_id: int,
        folder_id: int,
        item: ScannedFile,
        control: EngineControl,
    ) -> None:
        current = self._refresh_if_changed(storage_id, folder_id, item)
        if current.size > self._gateway.max_upload_bytes():
            raise BackupError(
                f"{item.relative} ({current.size} bytes) exceeds the current "
                "Telegram upload limit."
            )
        transfer = self._registry.start_upload(
            self._require_file_id(storage_id, current.relative),
            storage_id, current.size, str(current.path),
        )
        transfer = self._registry.transition(transfer.id, TransferState.UPLOADING)
        part_size = self._gateway.suggest_part_size(current.size)
        start_part = 0
        # Telegram stores upload parts per file id, so an interrupted upload can
        # only be continued while we still hold that id (same process). After a
        # crash recovery there is none and the file restarts cleanly.
        upload_id: Optional[int] = None
        attempts = 0
        posted_msg_id: Optional[int] = None

        def restart_upload() -> None:
            """Forgets the partial upload and its progress (used on retries)."""
            nonlocal start_part, upload_id
            start_part = 0
            upload_id = None
            self._registry.checkpoint(transfer.id, 0)

        while True:
            control.check_cancelled()
            last_done = [start_part * part_size]

            def on_progress(done: int) -> None:
                delta = done - last_done[0]
                last_done[0] = done
                if delta > 0:
                    self._limiter.consume(delta)
                self._registry.checkpoint(transfer.id, done)

            try:
                uploaded = self._gateway.upload(
                    current.path, progress=on_progress,
                    should_pause=control.should_pause,
                    is_cancelled=control.is_cancelled,
                    start_part=start_part, part_size=part_size, file_id=upload_id,
                )
                upload_id = uploaded.file_id
            except UploadPaused as exc:
                # Keep the Telegram file id so the paused file continues from its
                # checkpoint instead of being uploaded again from scratch.
                if upload_id is None:
                    upload_id = getattr(exc, "file_id", None)
                self._registry.pause(transfer.id)
                control.wait_if_paused()  # raises EngineCancelled on cancel
                transfer = self._registry.resume(transfer.id)
                transfer = self._registry.transition(transfer.id, TransferState.UPLOADING)
                checkpoint = self._registry_checkpoint(transfer.id)
                start_part = checkpoint // part_size
                continue
            except UploadCancelled:
                self._registry.cancel(transfer.id)
                raise EngineCancelled("cancelled during upload")
            except (OSError, ConnectionStateError) as exc:
                attempts = self._wait_for_network(transfer.id, attempts, str(exc))
                restart_upload()  # parts may have expired; restart safely
                transfer = self._requeue_upload(transfer.id)
                continue
            except Exception as exc:
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, str(exc))
                    raise
                logger.info(f"Retrying {item.relative} after error: {exc}")
                self._sleeper(min(2.0 ** attempts, 30.0))
                restart_upload()
                transfer = self._requeue_upload(transfer.id)
                continue
            try:
                sent = self._gateway.send_to_topic(chat_id, topic_id, uploaded, caption=item.name)
                posted_msg_id = sent.msg_id
            except (OSError, ConnectionStateError) as exc:
                attempts = self._wait_for_network(transfer.id, attempts, str(exc))
                restart_upload()
                transfer = self._requeue_upload(transfer.id)
                continue
            except Exception as exc:
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, str(exc))
                    raise
                self._sleeper(min(2.0 ** attempts, 30.0))
                transfer = self._requeue_upload(transfer.id)
                continue
            # Verify: cheap remote size check; on mismatch remove OUR OWN
            # just-posted message and restart (never leave silent corruption).
            self._registry.transition(transfer.id, TransferState.VERIFYING)
            try:
                doc = self._gateway.resolve_document(chat_id, posted_msg_id)
            except Exception as exc:
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, str(exc))
                    raise
                transfer = self._requeue_upload(transfer.id)
                continue
            if doc.size != current.size:
                try:
                    self._gateway.delete_messages(chat_id, [posted_msg_id])
                except Exception as exc:
                    logger.warning(f"Could not remove mismatched post: {exc}")
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, "remote size mismatch")
                    raise BackupError(f"Remote verification failed for {item.relative}.")
                restart_upload()
                transfer = self._requeue_upload(transfer.id)
                continue
            self._files.mark_backed_up(
                self._require_file_id(storage_id, current.relative), chat_id, posted_msg_id
            )
            self._files.record_message(storage_id, chat_id, posted_msg_id, topic_id,
                                       self._require_file_id(storage_id, current.relative))
            self._registry.transition(transfer.id, TransferState.COMPLETED)
            return

    def _refresh_if_changed(
        self, storage_id: int, folder_id: int, item: ScannedFile
    ) -> ScannedFile:
        try:
            stat = item.path.stat()
        except OSError as exc:
            raise BackupError(f"Source file vanished: {item.relative} ({exc})")
        if stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
            from teloude.core.scanner import hash_scanned as _hash

            current = ScannedFile(
                path=item.path, relative=item.relative, name=item.name,
                size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            )
            _hash(current)
            self._files.upsert(
                storage_id, folder_id, str(current.path), current.relative,
                current.name, current.size, current.mtime_ns / 1e9,
                current.sha256, current.fingerprint,
            )
            logger.info(f"Source changed during backup, re-hashed: {item.relative}")
            return current
        return item

    def _require_file_id(self, storage_id: int, relative: str) -> int:
        rec = self._files.get_by_path(storage_id, relative)
        if rec is None:  # pragma: no cover - defensive
            raise BackupError(f"File index row missing for {relative}.")
        return rec.id

    def _registry_checkpoint(self, transfer_id: int) -> int:
        rec = self._registry.active_record(transfer_id)
        return rec.done_bytes if rec else 0

    def _requeue_upload(self, transfer_id: int):
        rec = self._registry.active_record(transfer_id)
        assert rec is not None
        state = TransferState(rec.status)
        if state is TransferState.PAUSED:
            self._registry.resume(transfer_id)
        elif state in (TransferState.WAITING_FOR_NETWORK, TransferState.VERIFYING,
                       TransferState.FAILED):
            self._registry.transition(transfer_id, TransferState.QUEUED)
        return self._registry.transition(transfer_id, TransferState.UPLOADING)

    def _wait_for_network(self, transfer_id: int, attempts: int, error: str) -> int:
        attempts += 1
        if attempts > self._max_retries:
            self._registry.fail(transfer_id, f"network unreachable: {error}")
            raise ConnectionStateError(f"Network unreachable: {error}")
        self._registry.transition(transfer_id, TransferState.WAITING_FOR_NETWORK)
        self._sleeper(min(2.0 ** attempts, 30.0))
        return attempts

    def _refresh_storage_stats(self, storage_id: int) -> None:
        backed = [f for f in self._files.list_by_storage(storage_id) if f.is_backed_up]
        folders = self._folders.list_by_storage(storage_id)
        try:
            self._storages.update_stats(
                storage_id,
                sum(f.size for f in backed),
                len(backed),
                len(folders),
                utcnow(),
            )
        except Exception as exc:
            logger.warning(f"Could not refresh storage stats: {exc}")
