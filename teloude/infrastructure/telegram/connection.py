# teloude/infrastructure/telegram/connection.py
"""The one Telegram connection of the application.

Every feature - signing in, creating the session, uploading, downloading,
syncing a storage, searching - runs through the client this module builds, so
whatever proxy is configured here applies to all of them. Features never build
their own client and never handle proxies themselves.

The layer is Qt-free and Telethon-free: it drives ``TelethonTelegramClient``
(imported lazily, when a client is actually needed) through an injectable raw
client factory, so tests run it against scripted doubles without a network.

State model: ``ConnectionStatus`` is the client's own lifecycle (it includes
"authenticating"); ``ConnectionState`` is the four-state model the connection
indicator speaks, mapped from it.
"""
import logging
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Callable, List, Optional, Union

from .connection_state import ConnectionStatus
from .exceptions import ProxyConfigError
from .proxy import ProxyConfig, ProxySettingsStore
from .session_manager import TelethonSessionManager

logger = logging.getLogger("TelegramConnection")

DEFAULT_PROBE_TIMEOUT = 30.0


class ConnectionState(str, Enum):
    """The four states the indicator shows, in Telegram's terms."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"

    @classmethod
    def from_status(cls, status: ConnectionStatus) -> "ConnectionState":
        """Maps the client lifecycle onto the indicator's four states.

        "Authenticating" is a *connected* client that still needs a login code,
        so for the indicator it is a connection in progress.
        """
        if status is ConnectionStatus.READY:
            return cls.CONNECTED
        if status in (ConnectionStatus.CONNECTING, ConnectionStatus.AUTHENTICATING):
            return cls.CONNECTING
        if status is ConnectionStatus.ERROR:
            return cls.ERROR
        return cls.DISCONNECTED


@dataclass
class ConnectionSnapshot:
    """What the UI needs to show, without a secret in it."""

    state: ConnectionState = ConnectionState.DISCONNECTED
    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    detail: str = ""
    proxy_enabled: bool = False
    proxy: str = ""  # "host:port" or "" - never the secret

    def as_payload(self) -> dict:
        return {
            "state": self.state.value,
            "status": self.status.value,
            "detail": self.detail,
            "proxy_enabled": self.proxy_enabled,
            "proxy": self.proxy,
        }


@dataclass
class ProxyTestResult:
    """Outcome of "Test connection" / "Connect" on the proxy page."""

    ok: bool
    state: ConnectionState
    message: str
    proxy: str = ""
    connected_session: bool = False


def redact(message: str, *secrets: str) -> str:
    """Removes any occurrence of a secret from a message before it is shown.

    Third-party errors occasionally quote their whole configuration back.
    Nothing that came out of a proxy secret may reach a log line, a status
    label or an error dialog, so every message that could contain one goes
    through here first.
    """
    text = str(message or "")
    for secret in secrets:
        candidate = (secret or "").strip()
        for variant in {candidate, candidate.lower(), candidate.upper()}:
            if len(variant) >= 8:
                text = text.replace(variant, "<secret>")
    return text


class TelegramConnection:
    """Owns the proxy configuration and the client every feature uses.

    Create one per process. ``connect(phone)`` is the single entry point that
    produces a connected client; the application wires that callable into the
    rest of the stack (see ``ui/app.py``), which is what keeps authentication,
    upload, download, sync and search on one and the same configuration.
    """

    def __init__(
        self,
        api_id: Union[int, str],
        api_hash: str,
        session_dir: Union[str, Path],
        proxy_store: Optional[ProxySettingsStore] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        bus: Any = None,
    ):
        self._api_id = api_id
        self._api_hash = api_hash or ""
        self._session_dir = Path(session_dir) if session_dir else Path(".")
        self._store = proxy_store
        self._client_factory = client_factory
        self._probe_timeout = float(probe_timeout)
        self._bus = bus
        self._lock = threading.RLock()
        self._status = ConnectionStatus.DISCONNECTED
        self._detail = "Not connected to Telegram yet."
        self._client: Any = None
        self._had_client = False  # a live client existed at some point
        self._phone = ""
        self._listeners: List[Callable[[ConnectionSnapshot], None]] = []
        self._reconnect_hooks: List[Callable[[Any], None]] = []
        try:
            self._proxy = proxy_store.load() if proxy_store is not None else ProxyConfig()
        except Exception as exc:  # an unreadable row must not stop the app
            logger.warning("Could not read the saved proxy settings: %s", type(exc).__name__)
            self._proxy = ProxyConfig()

    # -- configuration -------------------------------------------------------
    def attach_proxy_store(self, store: ProxySettingsStore) -> ProxyConfig:
        """Adopts the settings-backed store and loads what it holds.

        The composition root builds the store once the database is open, so the
        connection can keep its policy (validate, protect, use) while storage
        stays where the rest of the application's storage lives.
        """
        with self._lock:
            self._store = store
            try:
                self._proxy = store.load()
            except Exception as exc:
                logger.warning("Could not read the saved proxy settings: %s", type(exc).__name__)
                self._proxy = ProxyConfig()
            return replace(self._proxy)

    def attach_bus(self, bus: Any) -> None:
        """Publishes status changes on the application event bus (once)."""
        with self._lock:
            self._bus = bus
        self._publish(self.snapshot())

    @property
    def proxy_store(self) -> Optional[ProxySettingsStore]:
        return self._store

    def proxy_mechanism(self) -> str:
        """How the stored secret is protected (DPAPI / labelled fallback)."""
        return self._store.mechanism if self._store is not None else "not stored"

    def proxy_config(self) -> ProxyConfig:
        """A copy: the caller cannot change the live configuration by accident."""
        with self._lock:
            return replace(self._proxy)

    def active_proxy(self) -> Optional[ProxyConfig]:
        """The proxy a new connection would use, or None for a direct one."""
        with self._lock:
            proxy = self._proxy.clean()
        if not proxy.enabled:
            return None
        if not proxy.is_complete():
            logger.warning("Proxy is enabled but incomplete; connecting directly.")
            return None
        return proxy

    def apply_proxy(self, config: ProxyConfig) -> ProxyConfig:
        """Validates, persists and activates a configuration.

        An enabled configuration must be valid; a disabled one may still be
        partial (the user is typing). Raises ProxyConfigError otherwise.
        """
        config = config.clean()
        if config.enabled:
            config.validate()
        if self._store is not None:
            config = self._store.save(config)
        with self._lock:
            self._proxy = config
        logger.info(
            "Proxy configuration applied: %s (enabled=%s)", config.describe(), config.enabled
        )
        if self._client is None:
            detail = (
                f"Proxy enabled: {config.endpoint()}"
                if config.enabled
                else "Proxy disabled; connecting to Telegram directly."
            )
        else:
            detail = (
                f"Proxy saved ({config.endpoint()}). Press Connect to use it now."
                if config.enabled
                else "Proxy disabled. Press Connect to switch back."
            )
        self._set_status(self._status, detail, force=True)
        return config

    def set_proxy_enabled(self, enabled: bool) -> ProxyConfig:
        """Enable/disable the saved proxy without touching its fields."""
        config = self.proxy_config()
        return self.apply_proxy(
            replace(config, enabled=bool(enabled))
        )

    # -- status --------------------------------------------------------------
    @property
    def status(self) -> ConnectionStatus:
        with self._lock:
            return self._status

    @property
    def state(self) -> ConnectionState:
        return ConnectionState.from_status(self.status)

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    def snapshot(self) -> ConnectionSnapshot:
        with self._lock:
            proxy = self._proxy
            return ConnectionSnapshot(
                state=ConnectionState.from_status(self._status),
                status=self._status,
                detail=self._detail,
                proxy_enabled=bool(proxy.enabled),
                proxy=proxy.endpoint(),
            )

    def add_listener(self, callback: Callable[[ConnectionSnapshot], None]) -> Callable:
        """Registers a status listener (called on the connecting thread).

        UI code listens on the event bus instead (``connection_state``), which
        the Qt bridge moves onto the UI thread.
        """
        with self._lock:
            self._listeners.append(callback)
        return callback

    def remove_listener(self, callback: Callable) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def add_reconnect_hook(self, hook: Callable[[Any], None]) -> Callable:
        """Called with the new client whenever a live client is replaced.

        The application uses this to re-point its gateways (upload, download,
        sync, search) at the new transport after a proxy switch.
        """
        with self._lock:
            self._reconnect_hooks.append(hook)
        return hook

    def _set_status(self, status: ConnectionStatus, detail: str = "", force: bool = False) -> None:
        with self._lock:
            if not force and status is self._status and (detail or "") == self._detail:
                return
            self._status = status
            self._detail = detail or self._default_detail(status)
            snapshot = self.snapshot()
        logger.info("Connection state: %s (%s)", snapshot.state.value, snapshot.detail)
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("A connection listener failed.")
        self._publish(snapshot)

    def _publish(self, snapshot: ConnectionSnapshot) -> None:
        bus = self._bus
        if bus is None:
            return
        try:
            bus.emit("connection_state", snapshot.as_payload())
        except Exception:
            logger.exception("Could not publish the connection state.")

    def _default_detail(self, status: ConnectionStatus) -> str:
        proxy = self.active_proxy()
        through = f" through {proxy.endpoint()}" if proxy is not None else ""
        if status is ConnectionStatus.READY:
            return f"Connected to Telegram{through}."
        if status is ConnectionStatus.CONNECTING:
            return f"Connecting to Telegram{through}..."
        if status is ConnectionStatus.AUTHENTICATING:
            return f"Connected to Telegram{through}; sign-in required."
        if status is ConnectionStatus.ERROR:
            return "Could not connect to Telegram."
        return "Not connected to Telegram yet."

    # -- clients -------------------------------------------------------------
    def create_client(self, phone: str, session_path: Optional[str] = None, proxy: Any = None):
        """Builds the client wrapper for ``phone`` with the active proxy.

        Telethon is imported here, not at module import time, so this module
        stays importable (and testable) on a machine without Telethon.
        """
        from .models import TelegramCredentials
        from .telethon_client import TelethonTelegramClient

        manager = TelethonSessionManager(session_dir=self._session_dir)
        path = Path(session_path) if session_path else manager.get_session_path(phone)
        credentials = TelegramCredentials(
            phone_number=phone or "", api_id=self._api_id, api_hash=self._api_hash
        )
        active = self.active_proxy() if proxy is None else proxy
        return TelethonTelegramClient(
            credentials,
            session_manager=manager,
            session_path=str(path),
            client_factory=self._client_factory,
            proxy=active,
        )

    def connect(self, phone: str):
        """Connects through the configured transport and returns the client.

        This is the callable the application hands to the rest of the stack, so
        the proxy configured here is the one authentication, session creation,
        uploads, downloads, syncs and searches all use.
        """
        client = self.create_client(phone)
        self._set_status(ConnectionStatus.CONNECTING)
        try:
            client.connect()
        except Exception as exc:
            detail = redact(str(exc) or type(exc).__name__, self._proxy.secret)
            self._set_status(ConnectionStatus.ERROR, detail)
            raise
        with self._lock:
            # A client built earlier is still wired into the application (the
            # gateways hold it), so this connection replaces it everywhere.
            replaced = self._had_client
            self._client = client
            self._had_client = True
            self._phone = phone
            status = client.get_status()
        self._set_status(status, redact(self._default_detail(status), self._proxy.secret))
        if replaced:
            self._rebind(client)
        return client

    def reconnect(self, phone: Optional[str] = None):
        """Drops the live client and connects again with the current proxy.

        Used after the proxy settings changed while signed in: the new client
        replaces the old one everywhere (see ``add_reconnect_hook``).
        """
        phone = phone or self._phone
        if not phone:
            raise ProxyConfigError("Sign in first, then the connection can be switched.")
        logger.info("Switching the Telegram connection (%s).", self.proxy_config().describe())
        self.disconnect()
        return self.connect(phone)

    def disconnect(self) -> None:
        """Closes the live client and returns to DISCONNECTED."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning("Telegram disconnect reported an error: %s", exc)
        self._set_status(ConnectionStatus.DISCONNECTED, "Disconnected from Telegram.")

    def _rebind(self, client: Any) -> None:
        for hook in list(self._reconnect_hooks):
            try:
                hook(client)
            except Exception:
                logger.exception("A reconnect hook failed.")

    # -- connection test -----------------------------------------------------
    def test_proxy(self, config: Optional[ProxyConfig] = None, timeout: Optional[float] = None) -> ProxyTestResult:
        """Checks a configuration against Telegram without touching the session.

        The probe runs on a throwaway session in a temporary directory, so a
        wrong proxy - or a probe that hangs - cannot disturb the signed-in
        session; it is time-boxed because an unreachable proxy can simply stop
        answering.
        """
        candidate = (config or self.proxy_config()).clean()
        if candidate.enabled:
            try:
                candidate.validate()
            except ProxyConfigError as exc:
                self._report_probe(ConnectionStatus.ERROR, str(exc), always=True)
                return ProxyTestResult(
                    ok=False, state=ConnectionState.ERROR, message=str(exc),
                    proxy=candidate.endpoint(),
                )
        else:
            candidate = replace(candidate, enabled=False)

        active = candidate if candidate.enabled else None
        # A test is the connection state while nothing is connected (that is the
        # usual case on the sign-in window); a live session keeps its own state
        # and the dialog shows the test result itself.
        manage_status = self._client is None
        self._report_probe(ConnectionStatus.CONNECTING, "Testing the connection to Telegram...",
                           always=manage_status)
        outcome: dict = {}
        probe_dir = tempfile.mkdtemp(prefix="teloude-probe-")

        def run_probe() -> None:
            client = None
            try:
                client = self.create_client(
                    "", session_path=str(Path(probe_dir) / "probe.session"), proxy=active
                )
                client.connect()
                outcome["status"] = client.get_status()
            except Exception as exc:
                outcome["error"] = str(exc) or type(exc).__name__
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass

        thread = threading.Thread(target=run_probe, name="teloude-proxy-probe", daemon=True)
        thread.start()
        thread.join(timeout if timeout is not None else self._probe_timeout)

        try:
            if thread.is_alive():
                budget = timeout if timeout is not None else self._probe_timeout
                message = (
                    f"No answer from {candidate.endpoint()} within {budget:g}s."
                    if candidate.enabled
                    else "Telegram did not answer in time."
                )
                self._report_probe(ConnectionStatus.ERROR, message, always=manage_status)
                return ProxyTestResult(
                    ok=False, state=ConnectionState.ERROR, message=message,
                    proxy=candidate.endpoint(),
                )
            if "error" in outcome:
                message = redact(outcome["error"], candidate.secret)
                self._report_probe(ConnectionStatus.ERROR, message, always=manage_status)
                return ProxyTestResult(
                    ok=False, state=ConnectionState.ERROR, message=message,
                    proxy=candidate.endpoint(),
                )
            # The probe session is deliberately not signed in: a completed
            # MTProto handshake is exactly what the proxy was asked to prove.
            message = (
                f"Connected through {candidate.endpoint()}."
                if candidate.enabled
                else "Connected to Telegram directly."
            )
            self._report_probe(ConnectionStatus.READY, message, always=manage_status)
            return ProxyTestResult(
                ok=True, state=ConnectionState.CONNECTED, message=message,
                proxy=candidate.endpoint(),
            )
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _report_probe(self, status: ConnectionStatus, detail: str, always: bool) -> None:
        """Shows a probe outcome on the shared status when asked to."""
        if always:
            self._set_status(status, detail)
        elif self._client is not None:
            logger.info("Connection test result: %s", detail)
