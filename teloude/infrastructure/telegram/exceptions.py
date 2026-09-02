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

(End of file - total 37 lines)