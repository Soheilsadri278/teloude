# teloude/infrastructure/telegram/auth.py
"""Interactive MTProto login flow (Phase 2): phone code + 2FA password.

Flow: send_code(phone) -> CODE_SENT -> sign_in_code(phone, code) ->
AUTHORIZED, or PASSWORD_NEEDED -> sign_in_password(password) -> AUTHORIZED.

Codes, passwords, and phone-code hashes are NEVER logged. The low-level
client is injected (Telethon's TelegramClient in production, scripted fakes
in tests); every method tolerates sync or async doubles via bridge.run_sync.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .bridge import map_rpc_error, run_sync
from .exceptions import AuthError, TeloudeTelegramError

logger = logging.getLogger("TelegramAuth")


def _telethon_auth_errors() -> tuple:
    try:
        from telethon import errors as tl_errors

        return (
            tl_errors.SessionPasswordNeededError,
            tl_errors.PhoneCodeInvalidError,
            tl_errors.PhoneCodeExpiredError,
            tl_errors.PasswordHashInvalidError,
            tl_errors.PhoneNumberInvalidError,
            tl_errors.PhoneNumberBannedError,
        )
    except ImportError:
        return ()


class AuthState(str, Enum):
    SIGNED_OUT = "signed_out"
    CODE_SENT = "code_sent"
    PASSWORD_NEEDED = "password_needed"
    AUTHORIZED = "authorized"


@dataclass
class AuthSession:
    phone: str
    state: AuthState = AuthState.SIGNED_OUT


class ITelegramAuth(ABC):
    """Abstract interactive login flow; UI and services depend only on this."""

    def __init__(self) -> None:
        self._session: Optional[AuthSession] = None

    @property
    def session(self) -> Optional[AuthSession]:
        return self._session

    @abstractmethod
    def send_code(self, phone: str) -> AuthState:
        """Requests a Telegram login code for phone. Returns CODE_SENT."""
        raise NotImplementedError

    @abstractmethod
    def sign_in_code(self, phone: str, code: str) -> AuthState:
        """Signs in with the login code. May return PASSWORD_NEEDED (2FA)."""
        raise NotImplementedError

    @abstractmethod
    def sign_in_password(self, password: str) -> AuthState:
        """Completes 2FA. Returns AUTHORIZED."""
        raise NotImplementedError

    @abstractmethod
    def sign_out(self) -> None:
        """Revokes the session and returns to SIGNED_OUT."""
        raise NotImplementedError

    @abstractmethod
    def is_authorized(self) -> bool:
        """True when the stored session is already authorized."""
        raise NotImplementedError


class TelethonAuth(ITelegramAuth):
    """ITelegramAuth over a Telethon TelegramClient (or a compatible double)."""

    def __init__(self, client: Any):
        super().__init__()
        self._client = client
        self._phone_code_hash: Optional[str] = None

    def send_code(self, phone: str) -> AuthState:
        try:
            sent = run_sync(self._client.send_code_request(phone))
        except Exception as exc:
            raise _auth_error(exc, "requesting a login code") from exc
        self._phone_code_hash = getattr(sent, "phone_code_hash", None)
        self._session = AuthSession(phone=phone, state=AuthState.CODE_SENT)
        logger.info("Login code requested.")
        return AuthState.CODE_SENT

    def sign_in_code(self, phone: str, code: str) -> AuthState:
        try:
            run_sync(
                self._client.sign_in(
                    phone, code, phone_code_hash=self._phone_code_hash
                )
            )
        except Exception as exc:
            mapped = _auth_error(exc, "signing in with the login code")
            if _is_password_needed(exc):
                self._session = AuthSession(phone=phone, state=AuthState.PASSWORD_NEEDED)
                logger.info("Two-step verification password required.")
                return AuthState.PASSWORD_NEEDED
            raise mapped from exc
        self._session = AuthSession(phone=phone, state=AuthState.AUTHORIZED)
        self._phone_code_hash = None
        logger.info("Signed in successfully.")
        return AuthState.AUTHORIZED

    def sign_in_password(self, password: str) -> AuthState:
        phone = self._session.phone if self._session else ""
        try:
            run_sync(self._client.sign_in(password=password))
        except Exception as exc:
            raise _auth_error(exc, "verifying the 2FA password") from exc
        self._session = AuthSession(phone=phone, state=AuthState.AUTHORIZED)
        logger.info("Two-step verification accepted.")
        return AuthState.AUTHORIZED

    def sign_out(self) -> None:
        try:
            run_sync(self._client.log_out())
        except Exception as exc:
            raise map_rpc_error(exc, "signing out") from exc
        self._session = None
        self._phone_code_hash = None
        logger.info("Signed out.")

    def is_authorized(self) -> bool:
        try:
            return bool(run_sync(self._client.is_user_authorized()))
        except Exception as exc:
            raise map_rpc_error(exc, "checking authorization") from exc


def _is_password_needed(exc: BaseException) -> bool:
    names = {type(exc).__name__}
    names.update(base.__name__ for base in type(exc).__mro__)
    if "SessionPasswordNeededError" in names:
        return True
    # Fallback for doubles that emulate the condition without Telethon.
    return getattr(exc, "password_needed", False) is True


def _auth_error(exc: BaseException, operation: str) -> TeloudeTelegramError:
    for cls in _telethon_auth_errors():
        if isinstance(exc, cls):
            return AuthError(_friendly_message(type(exc).__name__, operation))
    mapped = map_rpc_error(exc, operation)
    if isinstance(mapped, TeloudeTelegramError) and not isinstance(
        mapped, AuthError
    ):
        return AuthError(str(mapped), details=getattr(mapped, "details", None))
    return mapped


def _friendly_message(error_name: str, operation: str) -> str:
    messages = {
        "PhoneCodeInvalidError": "The code is incorrect. Check the Telegram message and try again.",
        "PhoneCodeExpiredError": "The code has expired. Request a new one.",
        "PasswordHashInvalidError": "The 2FA password is incorrect. Try again.",
        "PhoneNumberInvalidError": "The phone number looks invalid. Use international format (+...).",
        "PhoneNumberBannedError": "This phone number cannot be used with Telegram.",
    }
    detail = messages.get(error_name, f"Could not complete {operation}.")
    return f"{detail}"
