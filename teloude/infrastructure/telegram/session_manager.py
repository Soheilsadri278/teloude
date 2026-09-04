# teloude/infrastructure/telegram/session_manager.py
"""Telegram session management foundation (Phase 1.3).

CURRENT IMPLEMENTATION (Phase 1.3 foundation):
- File-based session location management for Telethon SQLite sessions.
- Configurable session directory (no hardcoded user-specific paths).
- Existing/missing session detection via session-file presence.
- No network access; no Telethon imports here (abstraction boundary).

FUTURE SECURITY HARDENING (explicitly NOT implemented yet):
- Windows DPAPI / Credential Manager protection of session secrets.
- The Telethon ``.session`` file currently rests on disk as created by
  Telethon itself. Do NOT treat this module as providing encrypted storage.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Union
import logging
import re

from teloude.config import default_session_dir
from .models import TelegramCredentials, SessionKeys
from .exceptions import SessionError

logger = logging.getLogger("SessionManager")


def sanitize_phone_number(phone_number: str) -> str:
    """Returns a filesystem-safe identifier derived from a phone number.

    Keeps digits only (``+98 912-345`` -> ``98912345``), which also makes
    equivalent formatting of the same number resolve to the same session.
    Raises SessionError when no digits remain (also blocks path traversal,
    since the result can never contain separators).
    """
    digits = re.sub(r"\D", "", phone_number or "")
    if not digits:
        raise SessionError(
            "Cannot derive a session file name: phone number contains no digits."
        )
    return digits


class ITelegramSessionManager(ABC):
    """
    ABSTRACT INTERFACE: Defines the contract for managing Telegram session
    files across operating systems. Core/application logic must depend only
    on this interface, never on Telethon session internals.
    """

    @abstractmethod
    def load_session(self, creds: TelegramCredentials) -> Optional[SessionKeys]:
        """
        Attempts to load existing session keys for the given credentials.
        Returns a SessionKeys object if a usable session exists, otherwise None.
        Must NOT raise when the session is simply missing (return None).
        """
        pass

    @abstractmethod
    def save_session(self, keys: SessionKeys) -> None:
        """
        Persists session bookkeeping (ensures the session directory exists and
        the path is usable). Must NOT write secrets; Telethon itself owns the
        session-file contents.
        """
        pass

    @abstractmethod
    def get_session_path(self, phone_number: str) -> Path:
        """Returns the full ``.session`` file path for a phone number (no I/O)."""
        pass

    @abstractmethod
    def session_exists(self, phone_number: str) -> bool:
        """Returns True when a session file already exists for a phone number."""
        pass

    @abstractmethod
    def clear_session(self, phone_number: str) -> None:
        """Deletes the session file for a phone number if present (logout/re-auth)."""
        pass


class TelethonSessionManager(ITelegramSessionManager):
    """File-based session manager for Telethon SQLite sessions.

    Layout: ``<session_dir>/<digits>.session`` where ``<digits>`` is the
    sanitized phone number. Telethon's ``SQLiteSession`` appends ``.session``
    itself when missing, so passing this full path to ``TelegramClient`` is
    safe (no double extension).
    """

    def __init__(self, session_dir: Optional[Union[str, Path]] = None):
        self._session_dir = (
            Path(session_dir).expanduser()
            if session_dir
            else default_session_dir()
        )

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def get_session_path(self, phone_number: str) -> Path:
        return self._session_dir / f"{sanitize_phone_number(phone_number)}.session"

    def session_exists(self, phone_number: str) -> bool:
        try:
            return self.get_session_path(phone_number).is_file()
        except SessionError:
            return False

    def load_session(self, creds: TelegramCredentials) -> Optional[SessionKeys]:
        path = self.get_session_path(creds.phone_number)
        if path.is_file():
            logger.info("Existing Telegram session found for reuse.")
            return SessionKeys(session_file=str(path), is_valid=True)
        logger.info("No existing Telegram session found; fresh login will be required.")
        return None

    def save_session(self, keys: SessionKeys) -> None:
        if not keys.session_file:
            raise SessionError("Cannot save session: empty session file path.")
        path = Path(keys.session_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionError(
                f"Cannot prepare session directory '{path.parent}'.", details=str(exc)
            ) from exc
        # NOTE: session-file bytes are written by Telethon on connect/login,
        # never here. This method only guarantees a writable location.
        logger.info("Telegram session location ready for persistence.")

    def clear_session(self, phone_number: str) -> None:
        path = self.get_session_path(phone_number)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise SessionError(
                f"Cannot delete session file '{path}'.", details=str(exc)
            ) from exc
        logger.info("Telegram session cleared.")


class DummySessionManager(ITelegramSessionManager):
    """In-memory session manager for local testing only - DO NOT USE IN PRODUCTION!"""

    def __init__(self, session_dir: Optional[Union[str, Path]] = None):
        self._session_dir = Path(session_dir).expanduser() if session_dir else Path(".")
        self._saved: set = set()

    def get_session_path(self, phone_number: str) -> Path:
        return self._session_dir / f"{sanitize_phone_number(phone_number)}.session"

    def session_exists(self, phone_number: str) -> bool:
        logger.warning("Using Dummy Session Manager. No actual secure loading takes place.")
        try:
            path = self.get_session_path(phone_number)
        except SessionError:
            return False
        return str(path) in self._saved or path.is_file()

    def load_session(self, creds: TelegramCredentials) -> Optional[SessionKeys]:
        logger.warning("Using Dummy Session Manager. No actual secure loading takes place.")
        # Simulation of successful load for testing purposes
        return SessionKeys(session_file="dummy_session", is_valid=True)

    def save_session(self, keys: SessionKeys):
        logger.info(f"Dummy session saved successfully to {keys.session_file}.")
        if keys.session_file:
            self._saved.add(str(keys.session_file))

    def clear_session(self, phone_number: str) -> None:
        logger.warning("Using Dummy Session Manager. Clearing in-memory session only.")
        try:
            self._saved.discard(str(self.get_session_path(phone_number)))
        except SessionError:
            pass
