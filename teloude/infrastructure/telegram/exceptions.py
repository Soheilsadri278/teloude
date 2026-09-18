# teloude/infrastructure/telegram/exceptions.py

from typing import Union

class TeloudeTelegramError(Exception):
    """Base exception for all Telegram-related errors in the application."""
    def __init__(self, message: str = "A general Telegram error occurred.", details: Union[str, None] = None):
        super().__init__(message)
        self.details = details

class AuthError(TeloudeTelegramError):
    """Raised when authentication fails (e.g., invalid credentials, incorrect code)."""
    def __init__(self, message: str = "Authentication failed.", details: Union[str, None] = None):
        super().__init__(message, details)

class ConnectionStateError(TeloudeTelegramError):
    """Raised when the application attempts an operation on a disconnected client."""
    def __init__(self, message: str = "Client is not connected or authenticated.", details: Union[str, None] = None):
        super().__init__(message, details)

class RateLimitExceeded(TeloudeTelegramError):
    """Raised when Telegram rate limits are hit (FloodWait)."""
    def __init__(self, message: str = "Rate limit exceeded.", retry_after: int = 60, details: Union[str, None] = None):
        super().__init__(message, details)
        self.retry_after = retry_after

class SessionExpiredError(AuthError):
    """The Telegram session is no longer valid; the user must sign in again."""

    def __init__(
        self,
        message: str = (
            "Your Telegram session has ended. Sign in again to continue."
        ),
        details: Union[str, None] = None,
    ):
        super().__init__(message, details)


class StorageUnavailableError(TeloudeTelegramError):
    """The storage group is gone, or the account lost access to it."""

    def __init__(
        self,
        message: str = (
            "The Telegram group for this storage is no longer available. It may "
            "have been deleted, or access to it was removed."
        ),
        details: Union[str, None] = None,
    ):
        super().__init__(message, details)


class RemoteItemMissingError(TeloudeTelegramError):
    """A message Teloude relies on is no longer on Telegram."""

    def __init__(
        self,
        message: str = (
            "This file's copy no longer exists on Telegram. Run a backup again "
            "to upload it."
        ),
        details: Union[str, None] = None,
    ):
        super().__init__(message, details)


class SessionError(TeloudeTelegramError):
    """Raised for Telegram session path, validation, or persistence problems."""
    def __init__(self, message: str = "Telegram session error.", details: Union[str, None] = None):
        super().__init__(message, details)


class ProxyConfigError(TeloudeTelegramError):
    """The proxy settings cannot be used as entered.

    The message is written for the user and never contains the proxy secret.
    """
    def __init__(self, message: str = "The proxy settings are not usable.", details: Union[str, None] = None):
        super().__init__(message, details)

#(End of file - total 37 lines)