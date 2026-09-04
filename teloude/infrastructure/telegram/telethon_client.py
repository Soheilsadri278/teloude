# teloude/infrastructure/telegram/telethon_client.py
"""Telethon-backed implementation of ITelegramClient (Phase 1.3 foundation).

Scope: session wiring, configurable session location, connection-state
lifecycle (DISCONNECTED -> CONNECTING -> AUTHENTICATING -> READY, ERROR on
failure) and error mapping. The full interactive login flow (phone code, 2FA
password entry) and file upload/restore arrive in later phases; upload and
message-fetch methods below remain explicit placeholders.

Telethon is imported ONLY in this module. No Telethon type may leak through
the ITelegramClient interface (public signatures use only stdlib types,
TelegramCredentials/SessionKeys, and ConnectionStatus).
"""
from pathlib import Path
from typing import Any, Callable, List, Optional
import logging

from .bridge import run_sync

try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    _TELETHON_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via monkeypatched tests
    TelegramClient = None  # type: ignore

    class FloodWaitError(Exception):  # fallback so `except FloodWaitError` stays valid
        """Fallback used only when Telethon is not installed."""

        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__("Telethon is not installed.")
            self.seconds = int(kwargs.get("seconds", 60))

    _TELETHON_AVAILABLE = False


from .client_interface import ITelegramClient
from .models import TelegramCredentials, SessionKeys
from .connection_state import ConnectionStatus
from .exceptions import AuthError, ConnectionStateError, RateLimitExceeded
from .session_manager import ITelegramSessionManager, TelethonSessionManager


logger = logging.getLogger("TelethonClient")

# Factory creating the low-level client. Receives (session_path, api_id,
# api_hash) and returns an object with connect()/is_user_authorized()/
# send_message()/disconnect() (each may be sync or async). Injectable so
# tests never touch the network.
ClientFactory = Callable[[str, int, str], Any]


def _default_client_factory(session_path: str, api_id: int, api_hash: str) -> Any:
    if not _TELETHON_AVAILABLE or TelegramClient is None:
        raise ConnectionStateError(
            "Telethon is not installed. Install it with `pip install telethon` "
            "to enable Telegram connectivity."
        )
    # SQLiteSession appends '.session' only when missing, so passing the full
    # '<digits>.session' path is safe (no double extension).
    return TelegramClient(session_path, api_id, api_hash)


# Alias kept for backward compatibility (shared runner lives in bridge.py).
_resolve = run_sync


class TelethonTelegramClient(ITelegramClient):

    def __init__(
        self,
        credentials: TelegramCredentials,
        session_manager: Optional[ITelegramSessionManager] = None,
        session_path: Optional[str] = None,
        client_factory: Optional[ClientFactory] = None,
    ):
        super().__init__(credentials)
        self._session_manager = session_manager
        if session_path:
            self._session_path = Path(session_path).expanduser()
        elif session_manager is not None:
            self._session_path = session_manager.get_session_path(
                credentials.phone_number
            )
        else:
            # Configurable default location; never a hardcoded 'teloude_session'.
            self._session_path = TelethonSessionManager().get_session_path(
                credentials.phone_number
            )
        self._factory: ClientFactory = client_factory or _default_client_factory
        try:
            self._client = self._factory(
                str(self._session_path),
                credentials.api_id,
                credentials.api_hash,
            )
        except ConnectionStateError:
            # Missing Telethon: keep a None client so connect() can report it
            # as a proper ConnectionStateError instead of failing at construction.
            self._client = None
        self._status = ConnectionStatus.DISCONNECTED

    @property
    def session_path(self) -> Path:
        """Filesystem path of the Telethon session file (no secrets exposed)."""
        return self._session_path

    def connect(self) -> None:
        if self._client is None:
            self._status = ConnectionStatus.ERROR
            raise ConnectionStateError(
                "Telethon is not installed. Install it with `pip install telethon` "
                "to enable Telegram connectivity."
            )
        self._status = ConnectionStatus.CONNECTING
        logger.info("Connecting to Telegram.")
        try:
            _resolve(self._client.connect())
        except RateLimitExceeded:
            self._status = ConnectionStatus.ERROR
            raise
        except FloodWaitError as exc:
            self._status = ConnectionStatus.ERROR
            raise RateLimitExceeded(
                "Telegram temporarily limited this operation. "
                "Teloude will retry automatically.",
                retry_after=int(getattr(exc, "seconds", 60)),
                details=str(exc),
            ) from exc
        except AuthError:
            self._status = ConnectionStatus.ERROR
            raise
        except ConnectionStateError:
            self._status = ConnectionStatus.ERROR
            raise
        except OSError as exc:
            self._status = ConnectionStatus.ERROR
            raise ConnectionStateError(
                "Could not reach Telegram. Check the internet connection; "
                "Teloude will retry automatically.",
                details=str(exc),
            ) from exc
        except Exception as exc:
            self._status = ConnectionStatus.ERROR
            raise ConnectionStateError(str(exc) or "Failed to connect to Telegram.") from exc

        # Low-level connection is up; determine whether the stored session is
        # already authorized or an interactive login is still required.
        try:
            if hasattr(self._client, "is_user_authorized"):
                authorized = _resolve(self._client.is_user_authorized())
            else:  # pragma: no cover - test doubles without the method
                authorized = True
        except FloodWaitError as exc:
            self._status = ConnectionStatus.ERROR
            raise RateLimitExceeded(
                "Telegram temporarily limited this operation.",
                retry_after=int(getattr(exc, "seconds", 60)),
                details=str(exc),
            ) from exc
        except Exception as exc:
            self._status = ConnectionStatus.ERROR
            raise ConnectionStateError(str(exc) or "Authorization check failed.") from exc

        if authorized:
            self._status = ConnectionStatus.READY
            logger.info("Telegram connected successfully.")
            if self._session_manager is not None:
                try:
                    self._session_manager.save_session(
                        SessionKeys(session_file=str(self._session_path), is_valid=True)
                    )
                except Exception as exc:
                    # Connectivity won, bookkeeping lost: stay READY but say so.
                    logger.warning(f"Connected, but session bookkeeping failed: {exc}")
        else:
            # Session exists at network level but needs login code / 2FA.
            # The interactive flow itself arrives in a later phase.
            self._status = ConnectionStatus.AUTHENTICATING
            logger.info("Telegram authorization required (login code / 2FA).")

    def disconnect(self) -> None:
        """Drops the connection and returns to DISCONNECTED (concrete helper)."""
        if self._client is not None and hasattr(self._client, "disconnect"):
            try:
                _resolve(self._client.disconnect())
            except Exception as exc:
                logger.warning(f"Telegram disconnect reported an error: {exc}")
        self._status = ConnectionStatus.DISCONNECTED

    def get_status(self) -> ConnectionStatus:
        return self._status

    def find_storage(self, storage_name: str) -> Optional[str]:
        # Phase 1.3 placeholder: storage discovery arrives with Phase 2.
        return None

    def send_message(self, recipient_id: str, message: str) -> bool:
        if self._status is not ConnectionStatus.READY:
            raise ConnectionStateError(
                "Client is not connected. Call connect() and wait for READY first."
            )
        if self._client is None:  # pragma: no cover - defensive
            raise ConnectionStateError("Telegram client is unavailable.")
        try:
            _resolve(self._client.send_message(recipient_id, message))
            return True
        except RateLimitExceeded:
            raise
        except FloodWaitError as exc:
            raise RateLimitExceeded(
                "Telegram temporarily limited this operation.",
                retry_after=int(getattr(exc, "seconds", 60)),
                details=str(exc),
            ) from exc
        except Exception as exc:
            # Never log message contents; length is enough for diagnostics.
            logger.error(f"Failed to send test message ({len(message)} chars): {exc}")
            return False

    def upload_file(self, local_path: str, target_id: str) -> None:
        raise NotImplementedError(
            "File upload arrives with the upload engine in a later phase."
        )

    def get_user_messages(
        self,
        topic_id: str,
        count: int = 10
    ) -> List[dict]:
        raise NotImplementedError(
            "Message fetching arrives with storage management in a later phase."
        )
