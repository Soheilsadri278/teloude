# teloude/application/services.py
"""Application services: Qt-free orchestration of engines and gateways.

The UI layer subscribes to the EventBus and forwards events to Qt signals;
services never import Qt. Long operations run on worker threads owned by the
services (daemon threads; the UI only observes events).
"""
import logging
import re
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from teloude.core.backup import BackupManager
from teloude.core.control import ControlState, EngineControl, RunOutcome
from teloude.core.duplicates import DuplicateResolver
from teloude.core.restore import (
    RestoreManager,
)
from teloude.core.search import SearchResult, search_files
from teloude.core.topics import parse_topic_name
from teloude.infrastructure.database import DatabaseManager
from teloude.infrastructure.repositories import (
    FileRecord,
    FileRepository,
    FolderRepository,
    SettingsRepository,
    StorageRecord,
    StorageRepository,
)
from teloude.infrastructure.telegram.auth import AuthState, ITelegramAuth
from teloude.infrastructure.telegram.exceptions import (
    SessionExpiredError,
    StorageUnavailableError,
    TeloudeTelegramError,
)
from teloude.infrastructure.telegram.storage import ITelegramStorage

logger = logging.getLogger("Services")


class ServiceError(Exception):
    """User-actionable application failure (message safe for UI display)."""


class EventBus:
    """Minimal synchronous publish/subscribe bus (UI adapts to Qt signals)."""

    def __init__(self):
        self._subs: Dict[str, List[Callable[[Any], None]]] = {}

    def subscribe(self, event: str, callback: Callable[[Any], None]) -> Callable[[Any], None]:
        self._subs.setdefault(event, []).append(callback)
        return callback

    def emit(self, event: str, payload: Any = None) -> None:
        for callback in list(self._subs.get(event, [])):
            try:
                callback(payload)
            except Exception:
                logger.exception(f"Event subscriber failed for '{event}'.")


def _active_state(handle: Optional["OperationHandle"]) -> Optional[ControlState]:
    """Current state of a live operation; None once it is not running."""
    if handle is None or not handle.thread.is_alive():
        return None
    return handle.control.state


def _emit_transfer_state(bus: EventBus, kind: str, state) -> None:
    """Publishes the real control state of a run (spec sections 17-18).

    Bug 3: the UI must show what the transfer is actually doing, so every
    transition the engine confirms - and the outcome it ended with - is
    broadcast as a plain string instead of being inferred from progress.
    """
    value = state.value if hasattr(state, "value") else str(state)
    bus.emit("transfer_state", {"kind": kind, "state": value})


def _outcome_of(cancelled: bool, failed) -> RunOutcome:
    if cancelled:
        return RunOutcome.CANCELLED
    return RunOutcome.COMPLETED_WITH_ERRORS if failed else RunOutcome.COMPLETED


def _report_session_expired(bus: EventBus, exc: BaseException) -> None:
    """Tells the UI the session is gone so it can offer a fresh sign-in.

    Spec §16: "If a session becomes invalid, Teloude must provide a clear
    re-authentication flow." Any service that sees a SessionExpiredError routes
    it here; the window reacts by reopening the sign-in dialog.
    """
    logger.warning(f"Telegram session is no longer valid: {exc}")
    bus.emit("auth_state", {
        "state": AuthState.SIGNED_OUT.value,
        "expired": True,
        "reason": str(exc),
    })


class AuthService:
    """Sign-in flow plus the non-secret marker that finds the saved session.

    Bug 2: the Telethon session file is named after the phone number
    (``<session_dir>/<digits>.session``), so a later launch needs that number to
    find it again. Only the digits are remembered - never the session, the
    password, the login code or the phone-code hash - and the session itself
    stays in the existing secure store (DPAPI on Windows).
    """

    LAST_PHONE_KEY = "telegram.last_phone"

    def __init__(self, auth: ITelegramAuth, bus: EventBus, settings=None):
        self._auth = auth
        self._bus = bus
        self._settings = settings

    @property
    def state(self) -> AuthState:
        session = self._auth.session
        if session is not None:
            return session.state
        return AuthState.AUTHORIZED if self._auth.is_authorized() else AuthState.SIGNED_OUT

    def is_authorized(self) -> bool:
        """True when Telegram accepts the current session (interface contract)."""
        return bool(self._auth.is_authorized())

    def remembered_phone(self) -> Optional[str]:
        """Digits of the phone whose session was last authorized, or None.

        No secret is stored or returned: the number only says which session file
        to look for, which is why it is kept in the local settings table.
        """
        if self._settings is None:
            return None
        return self._settings.get(self.LAST_PHONE_KEY) or None

    def _remember_phone(self, phone: str) -> None:
        if self._settings is None or not phone:
            return
        digits = re.sub(r"\D", "", str(phone))
        if digits:
            self._settings.set(self.LAST_PHONE_KEY, digits)

    def forget_phone(self) -> None:
        """Drops the marker (sign-out): the next launch must ask for a login."""
        if self._settings is not None:
            self._settings.delete(self.LAST_PHONE_KEY)

    def start_login(self, phone: str) -> AuthState:
        state = self._auth.send_code(phone)
        self._bus.emit("auth_state", {"state": state.value})
        return state

    def submit_code(self, phone: str, code: str) -> AuthState:
        state = self._auth.sign_in_code(phone, code)
        if state is AuthState.AUTHORIZED:
            self._remember_phone(phone)
        self._bus.emit("auth_state", {"state": state.value})
        return state

    def submit_password(self, password: str) -> AuthState:
        state = self._auth.sign_in_password(password)
        if state is AuthState.AUTHORIZED:
            # The phone is still known from start_login/submit_code.
            session = self._auth.session
            self._remember_phone(session.phone if session is not None else "")
        self._bus.emit("auth_state", {"state": state.value})
        return state

    def logout(self) -> None:
        self._auth.sign_out()
        self.forget_phone()
        self._bus.emit("auth_state", {"state": AuthState.SIGNED_OUT.value})

    def mark_session_expired(self, reason: str = "") -> None:
        """Records that Telegram rejected the session (revoked/deactivated)."""
        _report_session_expired(self._bus, reason or "The session is no longer valid.")


class StorageService:
    def __init__(
        self,
        storages: StorageRepository,
        folders: FolderRepository,
        files: FileRepository,
        gateway: ITelegramStorage,
        bus: EventBus,
    ):
        self._storages = storages
        self._folders = folders
        self._files = files
        self._gateway = gateway
        self._bus = bus

    def list(self) -> List[StorageRecord]:
        return self._storages.list_all()

    def create_storage(self, name: str) -> StorageRecord:
        name = (name or "").strip()
        if not name:
            raise ServiceError("Storage name must not be empty.")
        if self._storages.get_by_name(name) is not None:
            raise ServiceError(f"A storage named '{name}' already exists.")
        try:
            info = self._gateway.create_storage(name)
            root_topic = self._gateway.ensure_topic(info.chat_id, name, is_root=True)
        except SessionExpiredError as exc:
            _report_session_expired(self._bus, exc)
            raise ServiceError(str(exc)) from exc
        except TeloudeTelegramError as exc:
            raise ServiceError(f"Could not create the Telegram storage: {exc}") from exc
        storage_id = self._storages.create(name)
        self._storages.set_telegram(storage_id, info.chat_id, True)
        folder_id = self._folders.ensure(storage_id, "", name)
        self._folders.set_topic(folder_id, root_topic.topic_id, root_topic.title)
        self._bus.emit("storages_changed", {"storage_id": storage_id})
        record = self._storages.get(storage_id)
        assert record is not None
        return record

    def adopt_storage(self, name: str) -> StorageRecord:
        """Links an existing Telegram group (multi-PC) and rebuilds the index."""
        name = (name or "").strip()
        if not name:
            raise ServiceError("Storage name must not be empty.")
        existing = self._storages.get_by_name(name)
        if existing is not None:
            raise ServiceError(f"A storage named '{name}' already exists locally.")
        try:
            info = self._gateway.find_storage(name)
        except SessionExpiredError as exc:
            _report_session_expired(self._bus, exc)
            raise ServiceError(str(exc)) from exc
        except TeloudeTelegramError as exc:
            raise ServiceError(f"Could not reach Telegram: {exc}") from exc
        if info is None:
            raise ServiceError(f"No Telegram group 'Teloude - {name}' was found.")
        storage_id = self._storages.create(name)
        self._storages.set_telegram(storage_id, info.chat_id, True)
        try:
            self._gateway.ensure_forum(info.chat_id)
        except TeloudeTelegramError as exc:
            logger.warning(f"Could not ensure forum mode: {exc}")
        self.refresh_storage(storage_id)
        self._bus.emit("storages_changed", {"storage_id": storage_id})
        record = self._storages.get(storage_id)
        assert record is not None
        return record

    def refresh_storage(self, storage_id: int) -> int:
        """Re-imports topics and document listings into the local index."""
        storage = self._storages.get(storage_id)
        if storage is None or storage.telegram_chat_id is None:
            raise ServiceError("Storage is unknown or not linked to Telegram.")
        chat_id = storage.telegram_chat_id
        try:
            topics = self._gateway.list_topics(chat_id)
        except SessionExpiredError as exc:
            _report_session_expired(self._bus, exc)
            raise ServiceError(str(exc)) from exc
        except StorageUnavailableError as exc:
            raise ServiceError(
                f"{exc} Use 'Repair link' to create a new group, or delete this "
                "storage entry."
            ) from exc
        except TeloudeTelegramError as exc:
            raise ServiceError(f"Could not list Telegram topics: {exc}") from exc
        imported = 0
        for topic in topics:
            relative_dir = parse_topic_name(topic.title, storage.name)
            if relative_dir is None:
                relative_dir = topic.title  # foreign title: keep flat, lose nothing
            folder_name = relative_dir.split("/")[-1] if relative_dir else storage.name
            folder_id = self._folders.ensure(storage_id, relative_dir, folder_name)
            self._folders.set_topic(folder_id, topic.topic_id, topic.title)
            try:
                documents = self._gateway.list_topic_documents(chat_id, topic.topic_id)
            except TeloudeTelegramError as exc:
                logger.warning(f"Skipping topic '{topic.title}': {exc}")
                continue
            for doc in documents:
                relative_path = f"{relative_dir}/{doc.file_name}" if relative_dir else doc.file_name
                file_id = self._files.upsert(
                    storage_id, folder_id, "", relative_path, doc.file_name,
                    doc.size, None, None, None,
                )
                self._files.mark_backed_up(file_id, chat_id, doc.msg_id)
                self._files.record_message(storage_id, chat_id, doc.msg_id, topic.topic_id, file_id)
                imported += 1
        return imported

    def repair_storage(self, storage_id: int, confirm: bool = False) -> StorageRecord:
        """Re-creates the Telegram group for a storage whose group was deleted.

        The local index is kept: paths, sizes and hashes stay useful, so the next
        backup uploads everything again. Every cloud link of that storage is
        cleared first - claiming a copy that no longer exists would make restores
        fail one file at a time. Local files are never touched.

        Destructive (it changes where future backups go), so the caller must pass
        confirm=True after asking the user.
        """
        storage = self._storages.get(storage_id)
        if storage is None:
            raise ServiceError("Unknown storage.")
        if not confirm:
            raise ServiceError("Repairing a storage requires explicit confirmation.")
        try:
            info = self._gateway.create_storage(storage.name)
            root_topic = self._gateway.ensure_topic(info.chat_id, storage.name, is_root=True)
        except SessionExpiredError as exc:
            _report_session_expired(self._bus, exc)
            raise ServiceError(str(exc)) from exc
        except TeloudeTelegramError as exc:
            raise ServiceError(f"Could not create a new Telegram group: {exc}") from exc
        forgotten = self._files.forget_cloud_state(storage_id)
        self._storages.set_telegram(storage_id, info.chat_id, True)
        folder_id = self._folders.ensure(storage_id, "", storage.name)
        self._folders.set_topic(folder_id, root_topic.topic_id, root_topic.title)
        logger.info(
            f"Storage '{storage.name}' re-linked to a new group; "
            f"{forgotten} file(s) will be uploaded again."
        )
        self._bus.emit("storages_changed", {"storage_id": storage_id, "repaired": True})
        record = self._storages.get(storage_id)
        assert record is not None
        return record

    def delete_storage_cloud(self, storage_id: int) -> None:
        """Deletes the Telegram group AND the local index. Caller must confirm first."""
        storage = self._storages.get(storage_id)
        if storage is None:
            raise ServiceError("Unknown storage.")
        if storage.telegram_chat_id is not None:
            try:
                self._gateway.delete_storage(storage.telegram_chat_id)
            except StorageUnavailableError:
                logger.info(
                    f"Storage '{storage.name}' group was already gone on Telegram."
                )
            except SessionExpiredError as exc:
                _report_session_expired(self._bus, exc)
                raise ServiceError(str(exc)) from exc
            except TeloudeTelegramError as exc:
                raise ServiceError(f"Could not delete the Telegram storage: {exc}") from exc
        self._storages.delete(storage_id)
        self._bus.emit("storages_changed", {"storage_id": storage_id})


@dataclass
class OperationHandle:
    kind: str  # 'backup' | 'restore'
    control: EngineControl
    thread: threading.Thread


class BackupService:
    """Single-active-run backup orchestration (one logical file at a time)."""

    def __init__(self, manager: BackupManager, files: FileRepository, bus: EventBus):
        self._manager = manager
        self._files = files
        self._bus = bus
        self._current: Optional[OperationHandle] = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.thread.is_alive()

    @property
    def state(self) -> Optional[ControlState]:
        """Real state of the active run, or None when no run is in flight."""
        return _active_state(self._current)

    def start(
        self,
        storage_id: int,
        root: Path,
        policy: str = DuplicateResolver.SKIP_ALL,
        ask_callback=None,
        verify_content: bool = False,
    ) -> None:
        with self._lock:
            if self._current is not None and self._current.thread.is_alive():
                raise ServiceError("A backup is already running.")
            control = EngineControl()
            control.set_state_listener(
                lambda state: _emit_transfer_state(self._bus, "backup", state)
            )
            thread = threading.Thread(
                target=self._run,
                args=(storage_id, Path(root), policy, ask_callback, control,
                      bool(verify_content)),
                name="teloude-backup",
                daemon=True,
            )
            self._current = OperationHandle("backup", control, thread)
            thread.start()

    def pause(self) -> None:
        handle = self._require_handle()
        handle.control.pause()

    def resume(self) -> None:
        handle = self._require_handle()
        handle.control.resume()

    def cancel(self) -> None:
        handle = self._require_handle()
        handle.control.cancel()

    def _require_handle(self) -> OperationHandle:
        with self._lock:
            if self._current is None:
                raise ServiceError("No backup operation is active.")
            return self._current

    def _run(self, storage_id, root, policy, ask_callback, control: EngineControl,
             verify_content: bool = False) -> None:
        try:
            # Report the state the worker really starts in before doing anything.
            _emit_transfer_state(self._bus, "backup", control.state)
            plan = self._manager.plan(storage_id, root, verify_content=verify_content)
            self._bus.emit("backup_planned", {
                "files": len(plan.files), "unchanged": len(plan.unchanged),
            })
            resolver = DuplicateResolver(policy=policy, ask_callback=ask_callback)

            def on_progress(done_files, total_files, done_bytes, total_bytes, current):
                self._bus.emit("backup_progress", {
                    "done_files": done_files, "total_files": total_files,
                    "done_bytes": done_bytes, "total_bytes": total_bytes,
                    "current": current,
                })

            report = self._manager.run(plan, resolver=resolver, control=control, progress=on_progress)
            _emit_transfer_state(
                self._bus, "backup",
                _outcome_of(report.cancelled, report.failed),
            )
            self._bus.emit("backup_done", {
                "uploaded": report.uploaded, "skipped": report.skipped,
                "unchanged": report.unchanged,
                "failed": report.failed, "cancelled": report.cancelled,
            })
        except SessionExpiredError as exc:
            _emit_transfer_state(self._bus, "backup", RunOutcome.FAILED)
            _report_session_expired(self._bus, exc)
            self._bus.emit("backup_done", {
                "uploaded": 0, "skipped": 0,
                "failed": [("", str(exc))], "cancelled": False, "error": True,
            })
        except Exception:
            logger.exception("Backup operation failed.")
            _emit_transfer_state(self._bus, "backup", RunOutcome.FAILED)
            self._bus.emit("backup_done", {
                "uploaded": 0, "skipped": 0,
                "failed": [("", traceback.format_exc(limit=3))],
                "cancelled": False, "error": True,
            })


class RestoreService:
    """Restore orchestration for file sets, folders, or whole storages."""

    def __init__(
        self, manager: RestoreManager, files: FileRepository,
        storages: StorageRepository, bus: EventBus,
    ):
        self._manager = manager
        self._files = files
        self._storages = storages
        self._bus = bus
        self._current: Optional[OperationHandle] = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.thread.is_alive()

    @property
    def state(self) -> Optional[ControlState]:
        """Real state of the active run, or None when no run is in flight."""
        return _active_state(self._current)

    def start_files(
        self,
        file_ids: List[int],
        dest_dir: Path,
        collision: str = "ask",
        collision_callback=None,
    ) -> None:
        records = []
        for file_id in file_ids:
            rec = self._files.get(file_id)
            if rec is None:
                raise ServiceError(f"Unknown file id: {file_id}")
            records.append(rec)
        self._start(records, dest_dir, collision_callback)

    def start_storage(
        self, storage_id: int, dest_dir: Path, collision_callback=None
    ) -> None:
        storage = self._storages.get(storage_id)
        if storage is None:
            raise ServiceError("Unknown storage.")
        records = [f for f in self._files.list_by_storage(storage_id) if f.is_backed_up]
        if not records:
            raise ServiceError("This storage has no backed-up files to restore.")
        self._start(records, dest_dir, collision_callback)

    def pause(self) -> None:
        self._require_handle().control.pause()

    def resume(self) -> None:
        self._require_handle().control.resume()

    def cancel(self) -> None:
        self._require_handle().control.cancel()

    def _require_handle(self) -> OperationHandle:
        with self._lock:
            if self._current is None:
                raise ServiceError("No restore operation is active.")
            return self._current

    def _start(self, records: List[FileRecord], dest_dir: Path, collision_callback) -> None:
        with self._lock:
            if self._current is not None and self._current.thread.is_alive():
                raise ServiceError("A restore is already running.")
            control = EngineControl()
            control.set_state_listener(
                lambda state: _emit_transfer_state(self._bus, "restore", state)
            )
            thread = threading.Thread(
                target=self._run,
                args=(records, Path(dest_dir), collision_callback, control),
                name="teloude-restore",
                daemon=True,
            )
            self._current = OperationHandle("restore", control, thread)
            thread.start()

    def _run(self, records, dest_dir, collision_callback, control: EngineControl) -> None:
        try:
            # Report the state the worker really starts in before doing anything.
            _emit_transfer_state(self._bus, "restore", control.state)

            def on_progress(done_files, total_files, done_bytes, total_bytes, current):
                self._bus.emit("restore_progress", {
                    "done_files": done_files, "total_files": total_files,
                    "done_bytes": done_bytes, "total_bytes": total_bytes,
                    "current": current,
                })

            report = self._manager.restore_files(
                records, dest_dir, collision_callback=collision_callback,
                control=control, progress=on_progress,
            )
            _emit_transfer_state(
                self._bus, "restore",
                _outcome_of(report.cancelled, report.failed),
            )
            self._bus.emit("restore_done", {
                "restored": report.restored, "skipped": report.skipped,
                "failed": report.failed, "cancelled": report.cancelled,
            })
        except SessionExpiredError as exc:
            _emit_transfer_state(self._bus, "restore", RunOutcome.FAILED)
            _report_session_expired(self._bus, exc)
            self._bus.emit("restore_done", {
                "restored": 0, "skipped": 0,
                "failed": [("", str(exc))], "cancelled": False, "error": True,
            })
        except Exception:
            logger.exception("Restore operation failed.")
            _emit_transfer_state(self._bus, "restore", RunOutcome.FAILED)
            self._bus.emit("restore_done", {
                "restored": 0, "skipped": 0,
                "failed": [("", "Restore failed unexpectedly.")],
                "cancelled": False, "error": True,
            })


class SearchService:
    def __init__(self, db: DatabaseManager):
        self._db = db

    def search(
        self, query: str, storage_id: Optional[int] = None,
        backed_only: bool = False, limit: int = 200,
    ) -> List[SearchResult]:
        return search_files(self._db, query, storage_id, backed_only, limit)


SPEED_PRESETS = (None, 10.0, 5.0, 2.0)  # None == Unlimited


class SettingsService:
    """User preferences persisted in the settings table."""

    def __init__(self, settings: SettingsRepository):
        self._settings = settings

    def get_speed_limit_mbps(self) -> Optional[float]:
        raw = self._settings.get("speed_limit_mbps")
        if raw is None or raw == "":
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def set_speed_limit_mbps(self, mbps: Optional[float]) -> None:
        if mbps is not None and mbps <= 0:
            raise ServiceError("Speed limit must be positive.")
        self._settings.set("speed_limit_mbps", "" if mbps is None else str(mbps))

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._settings.get(key, default)

    def set(self, key: str, value: str) -> None:
        self._settings.set(key, value)


@dataclass
class Services:
    auth: AuthService
    storages: StorageService
    backup: BackupService
    restore: RestoreService
    search: SearchService
    settings: SettingsService
    bus: EventBus
