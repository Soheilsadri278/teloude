# teloude/ui/app.py
"""Composition root: builds the full application stack (real or offline)."""
import logging
import logging.handlers
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from teloude.config import AppConfig
from teloude.application.services import (
    AuthService,
    BackupService,
    EventBus,
    RestoreService,
    SearchService,
    Services,
    SettingsService,
    StorageService,
)
from teloude.core.backup import BackupManager
from teloude.core.restore import RestoreManager
from teloude.core.speed_limiter import SpeedLimiter, limit_from_mbps
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    SettingsRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.security.dpapi import SecureSessionStore, default_protector

logger = logging.getLogger("TeloudeApp")


@dataclass
class Repos:
    storages: StorageRepository
    folders: FolderRepository
    files: FileRepository
    transfers: TransferRepository
    settings: SettingsRepository


@dataclass
class AppContext:
    """Everything views need; built once at startup, torn down at exit."""

    config: AppConfig
    db: DatabaseManager
    repos: Repos
    registry: TransferRegistry
    services: Services
    bus: EventBus
    bridge: object = None
    asker: object = None
    tray: object = None
    session_store: Optional[SecureSessionStore] = None
    disconnect: Callable[[], None] = lambda: None
    preview_cache_dir: Path = field(default_factory=lambda: Path("."))
    backup_manager: object = None
    restore_manager: object = None

    def apply_speed_limit(self, limiter: SpeedLimiter) -> None:
        if self.backup_manager is not None:
            self.backup_manager.set_limiter(limiter)
        if self.restore_manager is not None:
            self.restore_manager.set_limiter(limiter)

    def shutdown(self) -> None:
        for service in (self.services.backup, self.services.restore):
            try:
                # Only a live run is cancelled. Cancelling a finished one would
                # broadcast a transfer_state for a run that is already over, and
                # that stale event would land on whichever page runs next.
                if service.is_running:
                    service.cancel()
            except Exception:
                pass
        detach = getattr(self.bridge, "detach", None)
        if callable(detach):
            try:
                detach()  # no more UI updates once the window is going away
            except Exception as exc:
                logger.warning(f"Bridge detach during shutdown reported: {exc}")
        try:
            self.disconnect()
        except Exception as exc:
            logger.warning(f"Disconnect during shutdown reported: {exc}")
        try:
            close_db_connection(self.db)
        except Exception as exc:
            logger.warning(f"DB close during shutdown reported: {exc}")
        if self.session_store is not None:
            try:
                self.session_store.lock()
            except Exception as exc:
                logger.warning(f"Session lock during shutdown reported: {exc}")


def _assemble(
    config: AppConfig,
    make_auth,
    storage_gateway,
    file_gateway,
    session_store: Optional[SecureSessionStore],
    disconnect: Callable[[], None],
) -> AppContext:
    config.get_data_dir().mkdir(parents=True, exist_ok=True)
    config.get_session_dir().mkdir(parents=True, exist_ok=True)
    if not Path(config.database_path).is_absolute():
        # Never scatter the index in the working directory (e.g. Program Files).
        config = config.model_copy(
            update={"database_path": str(config.get_data_dir() / config.database_path)}
        )
    db = DatabaseManager(config)
    if not db.initialize():
        raise RuntimeError("Could not initialize the local database.")
    repos = Repos(
        storages=StorageRepository(db),
        folders=FolderRepository(db),
        files=FileRepository(db),
        transfers=TransferRepository(db),
        settings=SettingsRepository(db),
    )
    registry = TransferRegistry(repos.transfers)
    bus = EventBus()
    # The auth service also owns the non-secret marker that locates the saved
    # session on the next launch (Bug 2), which lives in the settings table.
    auth = make_auth(bus, repos.settings)
    settings_service = SettingsService(repos.settings)
    backup_manager = BackupManager(
        repos.storages, repos.folders, repos.files, registry,
        storage_gateway, file_gateway,
        limiter=SpeedLimiter(limit_from_mbps(settings_service.get_speed_limit_mbps())),
    )
    restore_manager = RestoreManager(
        repos.files, registry, file_gateway,
        limiter=SpeedLimiter(limit_from_mbps(settings_service.get_speed_limit_mbps())),
    )
    backup_service = BackupService(backup_manager, repos.files, bus)
    restore_service = RestoreService(restore_manager, repos.files, repos.storages, bus)
    storage_service = StorageService(
        repos.storages, repos.folders, repos.files, storage_gateway, bus
    )
    wire = getattr(auth, "wire_bus", None)
    if callable(wire):
        wire(bus)
    services = Services(
        auth=auth,
        storages=storage_service,
        backup=backup_service,
        restore=restore_service,
        search=SearchService(db),
        settings=settings_service,
        bus=bus,
    )
    preview_cache = config.get_data_dir() / "previews"
    preview_cache.mkdir(parents=True, exist_ok=True)
    return AppContext(
        config=config, db=db, repos=repos, registry=registry, services=services,
        bus=bus, session_store=session_store, disconnect=disconnect,
        preview_cache_dir=preview_cache,
        backup_manager=backup_manager, restore_manager=restore_manager,
    )


def build_offline(config: Optional[AppConfig] = None) -> AppContext:
    """Builds a fully local stack on scripted fakes (dev smoke tests, no network)."""
    from teloude.infrastructure.telegram.fakes import (
        FakeAuth, FakeFileGateway, FakeStorageGateway,
    )

    fake_auth = FakeAuth()
    fake_auth.send_code("+0000000000")
    fake_auth.sign_in_code("+0000000000", "11111")
    session_store = SecureSessionStore(
        (config or AppConfig()).get_session_dir() / "offline.session",
        protector=default_protector(),
    )
    return _assemble(
        config or AppConfig(),
        lambda bus, settings: AuthService(fake_auth, bus, settings),
        FakeStorageGateway(), FakeFileGateway(),
        session_store, disconnect=lambda: None,
    )


def build_real(
    config: AppConfig, api_id: int, api_hash: str,
    connector: Callable[[str], object],
) -> AppContext:
    """Builds the production stack; `connector(phone)` returns a connected client.

    The session file is unlocked (DPAPI) before anything touches Telegram.
    """
    from teloude.infrastructure.telegram.auth import TelethonAuth
    from teloude.infrastructure.telegram.bridge import run_sync
    from teloude.infrastructure.telegram.files import TelethonFileGateway
    from teloude.infrastructure.telegram.storage import (
        TelethonStorageGateway,
        telethon_list_dialogs,
    )

    session_dir = config.get_session_dir()
    pending: dict = {}
    bus_cell: dict = {}

    def _prepare(phone: str, settings=None) -> AuthService:
        from teloude.infrastructure.telegram.session_manager import TelethonSessionManager

        manager = TelethonSessionManager(session_dir=session_dir)
        session_path = manager.get_session_path(phone)
        store = SecureSessionStore(session_path, protector=default_protector())
        if not store.unlock():
            raise RuntimeError("Could not unlock the saved Telegram session.")
        pending["store"] = store
        # connector() returns a connected TelethonTelegramClient; gateways and
        # the auth flow need its RAW Telethon client (request objects + auth
        # methods), never the wrapper itself.
        wrapper = connector(phone)
        raw = wrapper.underlying_client
        auth_gateway = TelethonAuth(raw)
        storage_gateway = TelethonStorageGateway(
            invoke=raw, list_dialogs=telethon_list_dialogs(raw),
        )
        file_gateway = TelethonFileGateway(
            invoke=raw, get_me=lambda: run_sync(raw.get_me()),
        )
        pending["gateways"] = (storage_gateway, file_gateway)
        pending["disconnect"] = wrapper.disconnect
        return AuthService(auth_gateway, bus_cell["bus"], settings)

    # The auth service resolves once the phone number is known (sign-in dialog,
    # or the number remembered from the last successful sign-in).
    def _make_auth(bus, settings) -> _LazyAuth:
        bus_cell["bus"] = bus
        lazy = _LazyAuth(_prepare, settings)
        lazy.wire_bus(bus)
        return lazy

    ctx = _assemble(
        config, _make_auth,
        _LazyGateway(pending, 0), _LazyGateway(pending, 1),
        session_store=None, disconnect=lambda: pending.get("disconnect", lambda: None)(),
    )
    ctx.session_store = _LazyStore(pending)
    return ctx


class _LazyAuth(AuthService):
    """AuthService resolved once the phone number is known (real mode)."""

    def __init__(self, prepare, settings=None):
        self._prepare = prepare
        self._settings = settings
        self._real: Optional[AuthService] = None

    def _resolved(self) -> AuthService:
        assert self._real is not None, "Sign-in has not started yet."
        return self._real

    def prepare(self, phone: str) -> "AuthService":
        # _prepare closes over the event bus captured in build_real.
        self._real = self._prepare(phone, self._settings)
        return self._real

    def wire_bus(self, bus) -> None:
        # Recorded for API symmetry; the bus is already closed over in build_real.
        self._bus = bus

    @property
    def state(self):
        return self._resolved().state

    def is_authorized(self) -> bool:
        return self._real.is_authorized() if self._real is not None else False

    def start_login(self, phone: str):
        return self.prepare(phone).start_login(phone)

    def submit_code(self, phone: str, code: str):
        return self._resolved().submit_code(phone, code)

    def submit_password(self, password: str):
        return self._resolved().submit_password(password)

    def logout(self) -> None:
        self._resolved().logout()


class _LazyGateway:
    """Placeholder gateway replaced once connected (delegates attribute access)."""

    def __init__(self, pending: dict, index: int):
        self._pending = pending
        self._index = index

    def _real(self):
        try:
            return self._pending["gateways"][self._index]
        except (KeyError, IndexError):
            from teloude.application.services import ServiceError

            raise ServiceError(
                "Not connected to Telegram yet. Sign in first."
            ) from None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._real(), name)


class _LazyStore:
    """Session-store handle resolved after sign-in starts."""

    def __init__(self, pending: dict):
        self._pending = pending

    def _real(self) -> Optional[SecureSessionStore]:
        return self._pending.get("store")

    @property
    def mechanism(self) -> str:
        real = self._real()
        return real.mechanism if real is not None else "No session yet."

    def lock(self) -> bool:
        real = self._real()
        return real.lock() if real is not None else False

    def unlock(self) -> bool:
        real = self._real()
        return real.unlock() if real is not None else True


def restore_saved_session(ctx) -> bool:
    """Reopens the session saved by an earlier run without asking anything.

    Bug 2: after phone -> code -> 2FA the session was written to
    ``<session_dir>/<digits>.session``, but the next launch had no way to find
    it (the file name contains the phone number, which nobody remembered), so
    every start showed the sign-in dialog again even though the session was
    still valid.

    This reuses the existing secure layer end to end - the remembered digits
    locate the file, ``SecureSessionStore`` unlocks it (DPAPI on Windows), the
    production connector attaches to it, and ``is_authorized()`` asks Telegram
    whether that stored session is still signed in. Nothing is assumed: a
    missing marker, an unreadable store, a revoked session or a network failure
    all return False and the caller falls back to the ordinary sign-in dialog.
    Returns True only when the app can go straight to the main window.
    """
    auth = ctx.services.auth
    phone = auth.remembered_phone()
    if not phone:
        return False
    prepare = getattr(auth, "prepare", None)
    if not callable(prepare):
        return False  # offline/development stack: there is no real session
    try:
        prepare(phone)
    except Exception as exc:
        # Never log the number or anything derived from the session itself.
        logger.warning(f"Could not open the saved Telegram session: {exc}")
        return False
    try:
        authorized = bool(auth.is_authorized())
    except Exception as exc:
        logger.warning(f"Could not validate the saved Telegram session: {exc}")
        return False
    if authorized:
        logger.info("Saved Telegram session restored; no sign-in needed.")
    else:
        logger.info("The saved Telegram session is no longer valid; sign-in required.")
    return authorized


_NOT_CONFIGURED_MESSAGE = (
    "Teloude is not configured: set the TELOUDE_API_ID and TELOUDE_API_HASH "
    "environment variables to your own my.telegram.org application credentials, "
    "then start it again."
)

_MISSING_GUI_MESSAGE = (
    "Teloude cannot start: its GUI dependency PySide6 is not available ({reason}). "
    "Install the project dependencies with:  pip install -e ."
)


def _can_show_startup_dialog() -> bool:
    """Whether a startup problem should be shown in a window.

    An installed release is started from a Start Menu shortcut, so there is no
    terminal to print to: a window is the only way the reason can reach the user.
    Windows is the target platform and a double-clicked shortcut always has an
    interactive session behind it. Everywhere else - a source checkout, a
    container, a scripted smoke test - the message stays on the console and in
    the log file, so an automated run keeps its documented exit code instead of
    waiting for somebody to click OK.
    """
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def _show_startup_dialog(message: str) -> None:
    """Shows a startup problem in a window; best effort, never raises."""
    if not _can_show_startup_dialog():
        return
    try:
        from PySide6 import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QtWidgets.QMessageBox.critical(None, "Teloude", message)
        del app
    except Exception:  # pragma: no cover - depends on the desktop session
        logger.debug("Could not show the startup message in a window.", exc_info=True)


def startup_problem(offline: bool) -> Optional[tuple]:
    """What stops this process from starting, or None when it can run.

    Deliberately runs before any GUI import, in this order:

    1. configuration (a real run needs Telegram API credentials),
    2. the GUI dependency itself.

    Checking configuration first matters: a user asking "why does it not start?"
    must see "set TELOUDE_API_ID" even on a machine where PySide6 is missing, and
    a missing dependency must read as a sentence rather than as an ImportError
    traceback. Returns ``(exit_code, message)``.
    """
    if not offline:
        from teloude.config import TELEGRAM_API_ID, TELEGRAM_API_HASH

        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            return 2, _NOT_CONFIGURED_MESSAGE
    try:
        import PySide6  # noqa: F401  (availability probe, imported again below)
    except ImportError as exc:
        return 3, _MISSING_GUI_MESSAGE.format(reason=exc)
    return None


def run(argv=None) -> int:
    """Application entry point: parses flags, signs in, shows the main window."""
    import argparse

    parser = argparse.ArgumentParser(prog="teloude", description="Teloude Telegram Backup")
    parser.add_argument("--offline", action="store_true",
                        help="Run against local fakes (no network, for development).")
    parser.add_argument("--data-dir", default=None, help="Override the data directory.")
    parser.add_argument("--minimized", action="store_true",
                        help="Start minimized to the system tray (for autostart).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    config = AppConfig(data_dir=args.data_dir) if args.data_dir else AppConfig()
    try:
        log_dir = config.get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "teloude.log", maxBytes=2_000_000, backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s: %(message)s"
        ))
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        print(f"Could not set up file logging: {exc}")

    problem = startup_problem(args.offline)
    if problem is not None:
        # Print for the console, log it, and - in a packaged build, which has no
        # console at all - say it in a window as well: the log file and the
        # dialog are the only places the reason can reach a user who started
        # Teloude from a Start Menu shortcut.
        code, message = problem
        logger.error(message)
        print(message)
        if code == 2:
            _show_startup_dialog(message)
        return code

    from PySide6 import QtWidgets
    from teloude.ui import theme
    from teloude.ui.auth_dialog import AuthDialog
    from teloude.ui.bridge import ServiceBridge
    from teloude.ui.dialogs import UiThreadAsker
    from teloude.ui.main_window import MainWindow
    from teloude.ui.tray import TrayController

    qt_app = QtWidgets.QApplication(sys.argv)

    if args.offline:
        ctx = build_offline(config)
    else:
        from teloude.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
        from teloude.infrastructure.telegram.telethon_client import TelethonTelegramClient
        from teloude.infrastructure.telegram.models import TelegramCredentials
        from teloude.infrastructure.telegram.session_manager import TelethonSessionManager

        # startup_problem() already proved both credentials are present.
        def connector(phone: str):
            manager = TelethonSessionManager(session_dir=config.get_session_dir())
            creds = TelegramCredentials(
                phone_number=phone, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH,
            )
            client = TelethonTelegramClient(
                creds, session_manager=manager,
                session_path=str(manager.get_session_path(phone)),
            )
            client.connect()
            return client

        ctx = build_real(config, TELEGRAM_API_ID, TELEGRAM_API_HASH, connector)

    # The Apple-inspired design system: light by default, dark when the user
    # chose it on the Settings page (stored in the existing settings table).
    theme.apply_theme(qt_app, dark=theme.stored_appearance(ctx.services) == "dark")
    logger.info(f"Appearance: {theme.tokens().name} mode.")

    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()

    if not args.offline and not ctx.services.auth.is_authorized():
        # A session from a previous run must be reused silently; the dialog is
        # only for a genuinely missing, invalid or revoked session (Bug 2).
        if not restore_saved_session(ctx):
            dialog = AuthDialog(ctx.services.auth)
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                ctx.shutdown()
                return 0

    def recover_in_background() -> None:
        try:
            requeued = ctx.backup_manager.recover_pending()
            pruned = ctx.repos.transfers.prune_history()
            if requeued:
                logger.info(f"Recovered {requeued} interrupted transfer(s) from last run.")
            if pruned:
                logger.info(f"Trimmed {pruned} old transfer history row(s).")
        except Exception as exc:
            logger.warning(f"Startup recovery reported: {exc}")

    threading.Thread(target=recover_in_background, name="startup-recovery",
                     daemon=True).start()

    window = MainWindow(ctx)

    def safe_exit() -> None:
        ctx.shutdown()
        qt_app.quit()

    tray = TrayController(
        on_open=lambda: (window.show(), window.raise_(), window.activateWindow()),
        on_pause_all=lambda: _ignore_errors(ctx.services.backup.pause),
        on_resume_all=lambda: _ignore_errors(ctx.services.backup.resume),
        on_progress=lambda: (window.show(), window._goto("Transfers")),
        on_exit=safe_exit,
    )
    ctx.tray = tray
    if tray.is_supported:
        tray.show()
    if args.minimized and tray.is_supported:
        tray.show_message("Teloude", "Teloude is running in the system tray.")
    else:
        window.show()
    code = qt_app.exec()
    ctx.shutdown()
    return int(code)


def _ignore_errors(fn) -> None:
    try:
        fn()
    except Exception:
        pass

