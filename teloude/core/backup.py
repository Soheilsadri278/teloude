# teloude/core/backup.py
"""Backup engine: scan -> duplicates -> topics -> chunked upload -> verify.

One logical file at a time; internal chunking handled by the file gateway.
Source files are only ever read. Every state change persists through
TransferRegistry, so a restart resumes from checkpoints (same-session part
continuation where possible, safe full-file restart otherwise).

Path namespace (spec sections 8, 24, 35): everything a run indexes is stored
relative to the *storage*, anchored at the selected root folder's name. Backing
up C:/Users/me/Downloads therefore indexes "Downloads/file1.txt" and
"Downloads/Subfolder/file3.pdf", never a bare "file1.txt":

- the restore destination gets the selected root folder back
  (<destination>/Downloads/file1.txt), so the restored tree mirrors the source,
- each root folder of a storage maps to its own Telegram forum topic
  (<storage> / Downloads), and a second root folder can never fall into the
  first one's topic,
- two root folders that contain equal relative paths stay distinct records
  instead of overwriting each other in the index.

The selected root folder name is therefore part of the persistent
folder-to-topic relationship in the local database (spec section 8: the local
index is authoritative, topic titles only encode it).
"""
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from teloude.core.control import EngineCancelled, EngineControl
from teloude.core.errors import (
    is_network_error,
    local_failure_message,
    path_is_directory,
)
from teloude.infrastructure.telegram.exceptions import SessionExpiredError
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


_SESSION_EXPIRED_MESSAGE = (
    "Your Telegram session has ended. Sign in again to continue."
)


def _readable_error(exc: Exception, path=None) -> str:
    """Turns a filesystem error into text a user can act on."""
    if path_is_directory(path):
        # Windows reports EACCES when a directory is opened, POSIX EISDIR: in both
        # cases the honest answer is that the file was replaced by a folder.
        return "There is a folder where this file was, so it was not uploaded."
    if isinstance(exc, PermissionError):
        return "Could not read this file (permission denied)."
    if isinstance(exc, FileNotFoundError):
        return "The file disappeared before it could be read."
    if isinstance(exc, InterruptedError):
        return "Reading this file was interrupted."
    return f"Could not read this file: {exc}"


def backup_root_name(root: Path) -> str:
    """Name of the selected backup root, used as the storage-relative anchor.

    Never empty: a drive or filesystem root ("D:\\" on Windows, "/" elsewhere)
    has no name of its own, so its anchor becomes the drive (or "root") - a
    deterministic label that keeps the index, the topics and the restore
    destination stable across runs.
    """
    path = Path(root)
    name = path.name.strip()
    if name:
        return name
    anchor = path.anchor.strip("\\/").strip(":").strip()
    return anchor or "root"


def path_below(root: Path, path: str) -> Optional[str]:
    """The storage-relative part of ``path`` inside ``root``, or None.

    Compared as text (Windows paths case-insensitively) so the spelling of the
    row decides the result instead of a filesystem lookup: a path outside the
    selected root never matches, and neither does a path that only shares a
    prefix with it ("/data/root2" is not inside "/data/root").
    """
    local = str(path).replace("\\", "/")
    anchor = str(root).replace("\\", "/").rstrip("/") or "/"
    prefix = anchor + "/"
    if not os.path.normcase(local).startswith(os.path.normcase(prefix)):
        return None
    return local[len(prefix):]


@dataclass
class BackupPlan:
    storage_id: int
    storage_name: str
    root: Path
    root_name: str = ""
    files: List[ScannedFile] = field(default_factory=list)
    duplicates: List[DuplicateMatch] = field(default_factory=list)
    # Files already backed up whose content has not changed: uploading them
    # again would waste quota and duplicate every message on Telegram.
    unchanged: List[str] = field(default_factory=list)
    # Files whose content changed: relative path -> (chat_id, msg_id) of the
    # copy being replaced, so the old message can be removed once the new one is
    # verified.
    superseded: Dict[str, tuple] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class BackupReport:
    total: int = 0
    uploaded: int = 0
    skipped_duplicates: int = 0
    unchanged: int = 0  # already backed up and untouched since last time
    failed: List[tuple] = field(default_factory=list)  # (relative_path, error)
    cancelled: bool = False

    @property
    def skipped(self) -> int:
        """Everything that did not need uploading."""
        return self.skipped_duplicates + self.unchanged


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
        part_bytes: Optional[int] = None,
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
        # Configured transfer chunk (MTProto part size). None keeps the
        # gateway's own suggestion; the application passes the user setting.
        self._part_bytes = part_bytes

    def set_limiter(self, limiter: SpeedLimiter) -> None:
        """Swaps the rate limiter (applied to subsequently uploaded bytes)."""
        self._limiter = limiter

    # -- planning ------------------------------------------------------
    def plan(
        self,
        storage_id: int,
        root: Path,
        progress: Optional[Callable[[int, int], None]] = None,
        verify_content: bool = False,
    ) -> BackupPlan:
        """Classifies every local file as new, unchanged, or superseded.

        Unchanged detection is tiered (spec §21): file size + modification time
        decide first, and SHA-256 is computed whenever either changed. Pass
        ``verify_content=True`` to force the full hash for every file - slower,
        but it also catches an edit that kept the size and the mtime.
        """
        storage = self._storages.get(storage_id)
        if storage is None:
            raise BackupError(f"Unknown storage id: {storage_id}")
        self._failed_to_read: List[tuple] = []
        root = Path(root)
        root_name = backup_root_name(root)
        self._adopt_legacy_rows(storage_id, root, root_name)
        scanned = scan_directory(root)
        # Anchor every relative path at the selected root folder (spec sections
        # 8, 24): the index, the topic mapping and the restore destination all
        # need to know which folder the run came from.
        for item in scanned:
            item.relative = f"{root_name}/{item.relative}"
        total = len(scanned)
        usable: List[ScannedFile] = []
        unchanged: List[str] = []
        superseded: Dict[str, tuple] = {}
        for i, item in enumerate(scanned):
            existing = self._files.get_by_path(storage_id, item.relative)
            indexed = bool(existing is not None and existing.is_backed_up
                           and existing.sha256 and existing.telegram_msg_id)
            if indexed and not verify_content and existing.size == item.size \
                    and existing.fingerprint == item.fingerprint:
                # Cheap identity hit: size and mtime are exactly what was
                # indexed, so the bytes cannot have changed (spec §21). One
                # open() still proves the file is readable before we skip it.
                try:
                    with open(item.path, "rb"):
                        pass
                except OSError as exc:
                    logger.warning(f"Cannot read {item.relative}: {exc}")
                    self._failed_to_read.append(
                        (item.relative, _readable_error(exc, item.path))
                    )
                    if progress is not None:
                        progress(i + 1, total)
                    continue
                item.sha256 = existing.sha256
                self._files.upsert(
                    storage_id, None, str(item.path), item.relative, item.name,
                    item.size, existing.mtime, item.sha256, item.fingerprint,
                )
                unchanged.append(item.relative)
                if progress is not None:
                    progress(i + 1, total)
                continue
            try:
                hash_scanned(item)
            except (OSError, PermissionError) as exc:
                # A file we cannot read (locked by another program, permissions)
                # must not sink the whole run: it is reported and skipped.
                logger.warning(f"Cannot read {item.relative}: {exc}")
                self._failed_to_read.append(
                    (item.relative, _readable_error(exc, item.path))
                )
                if progress is not None:
                    progress(i + 1, total)
                continue
            same_content = bool(
                indexed and existing.size == item.size
                and existing.sha256 == item.sha256
            )
            if indexed:
                if same_content:
                    self._files.upsert(
                        storage_id, None, str(item.path), item.relative, item.name,
                        item.size, item.mtime_ns / 1e9, item.sha256, item.fingerprint,
                    )
                    unchanged.append(item.relative)
                    if progress is not None:
                        progress(i + 1, total)
                    continue
                superseded[item.relative] = (
                    existing.telegram_chat_id, existing.telegram_msg_id,
                )
            self._files.upsert(
                storage_id, None, str(item.path), item.relative, item.name,
                item.size, item.mtime_ns / 1e9, item.sha256, item.fingerprint,
            )
            usable.append(item)
            if progress is not None:
                progress(i + 1, total)
        scanned = usable

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
            root=root, root_name=root_name, files=scanned, duplicates=matches,
            unchanged=unchanged, superseded=superseded,
        )

    def _adopt_legacy_rows(self, storage_id: int, root: Path, root_name: str) -> int:
        """Lifts index rows that predate the root folder being part of the path.

        Rows written by an older build carry the path a file had *inside* the
        folder the user picked ("Sub/file.txt"), which is what flattened restores
        and let two root folders share a topic. The rows themselves are valid -
        they point at real cloud copies - so a run of the same root re-keys them
        to "<root name>/Sub/file.txt" instead of re-uploading or deleting them.

        Only an exact match is adopted: the row's local path must sit inside the
        selected root *and* its stored path must be exactly that file's path
        relative to the root. A row that already has an anchored twin, or that
        cannot be matched unambiguously, is left alone.
        """
        adopted = 0
        for record in self._files.list_by_storage(storage_id):
            if not record.local_path:
                continue
            inside = path_below(root, record.local_path)
            if not inside or inside != record.relative_path:
                continue
            target = f"{root_name}/{inside}"
            if self._files.get_by_path(storage_id, target) is not None:
                continue  # already indexed under its root: that row is the live one
            parent = str(Path(target).parent).replace("\\", "/")
            folder_id = self._folders.ensure(
                storage_id, parent, Path(parent).name or root_name
            )
            self._files.move_relative_path(record.id, target, folder_id)
            adopted += 1
        if adopted:
            logger.info(
                f"Re-anchored {adopted} index entr(ies) under '{root_name}' "
                f"(created before {root_name} was part of the stored path)."
            )
        return adopted

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
        report.unchanged = len(plan.unchanged)
        # files the planner could not read are reported as failures, not hidden
        report.failed.extend(getattr(self, "_failed_to_read", []) or [])
        self._failed_to_read = []
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
                    self._backup_one(
                        plan.storage_id, chat_id, topic_id, folder_id, item, control,
                        superseded=plan.superseded.get(item.relative),
                    )
                    report.uploaded += 1
                except EngineCancelled:
                    raise
                except SessionExpiredError:
                    # Every remaining file would fail the same way; stop and let
                    # the services layer ask the user to sign in again.
                    report.failed.append((item.relative, _SESSION_EXPIRED_MESSAGE))
                    raise
                except Exception as exc:
                    logger.warning(f"Backup failed for {item.relative}: {exc}")
                    if isinstance(exc, OSError):
                        message = local_failure_message(
                            exc, item.relative, path=item.path
                        )
                    else:
                        message = str(exc)
                    report.failed.append((item.relative, message))
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
        superseded: Optional[tuple] = None,
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
        part_size = self._part_bytes or self._gateway.suggest_part_size(current.size)
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
            except SessionExpiredError as exc:
                # Retrying an expired session just burns retries. Propagate it so
                # the run stops here and the services layer asks for a new login.
                self._registry.fail(transfer.id, str(exc))
                raise
            except (OSError, ConnectionStateError) as exc:
                if not is_network_error(exc):
                    # Permission/disk problems never fix themselves: report the
                    # real cause once instead of retrying blindly.
                    message = local_failure_message(
                        exc, item.relative, path=item.path
                    )
                    self._registry.fail(transfer.id, message)
                    raise BackupError(message) from exc
                attempts = self._wait_for_network(transfer.id, attempts, str(exc))
                restart_upload()  # parts may have expired; restart safely
                transfer = self._requeue_upload(transfer.id)
                continue
            except Exception as exc:
                attempts += 1
                if attempts > self._max_retries:
                    self._registry.fail(transfer.id, str(exc))
                    raise
                logger.warning(
                    f"Upload attempt {attempts} for {item.relative} failed: {exc}"
                )
                self._sleeper(min(2.0 ** attempts, 30.0))
                restart_upload()
                transfer = self._requeue_upload(transfer.id)
                continue
            try:
                sent = self._gateway.send_to_topic(chat_id, topic_id, uploaded, caption=item.name)
                posted_msg_id = sent.msg_id
            except (OSError, ConnectionStateError) as exc:
                if not is_network_error(exc):
                    message = local_failure_message(
                        exc, item.relative, path=item.path
                    )
                    self._registry.fail(transfer.id, message)
                    raise BackupError(message) from exc
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
            if superseded:
                # The file changed: its previous cloud copy is now dead weight.
                # Remove it AFTER the replacement is verified, and never touch
                # anything local (spec §20).
                old_chat_id, old_msg_id = superseded
                try:
                    self._gateway.delete_messages(old_chat_id, [old_msg_id])
                    logger.info(
                        f"Removed the previous Telegram copy of {current.relative}."
                    )
                except Exception as exc:
                    logger.warning(
                        f"Could not remove the older copy of {current.relative}: {exc}"
                    )
            self._registry.transition(transfer.id, TransferState.COMPLETED)
            return

    def _refresh_if_changed(
        self, storage_id: int, folder_id: int, item: ScannedFile
    ) -> ScannedFile:
        try:
            stat = item.path.stat()
        except OSError as exc:
            raise BackupError(f"Source file vanished: {item.relative} ({exc})")
        if item.path.is_dir():
            # The file was replaced by a folder while the run was in flight.
            # Opening it raises EISDIR on POSIX and a confusing EACCES on
            # Windows, so name the real condition instead of leaking either.
            raise BackupError(
                f"{item.relative}: there is a folder where this file was, so it "
                "cannot be uploaded."
            )
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
        """Puts a transfer back in the queue for another attempt.

        Covers every state a retry can start from - including the in-flight
        UPLOADING state, which is where non-network failures (Telegram rate
        limits, unexpected replies) land.
        """
        rec = self._registry.active_record(transfer_id)
        assert rec is not None
        state = TransferState(rec.status)
        if state is TransferState.PAUSED:
            self._registry.resume(transfer_id)
        elif state in (TransferState.WAITING_FOR_NETWORK, TransferState.VERIFYING,
                       TransferState.FAILED, TransferState.UPLOADING):
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
