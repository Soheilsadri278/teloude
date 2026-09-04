# teloude/infrastructure/telegram/bridge.py
"""Shared plumbing for the Telegram infrastructure layer (no UI/core imports).

- run_sync: drives Telethon's coroutines from synchronous foundation code.
- map_rpc_error: translates Telethon RPC failures into Teloude's exception
  hierarchy with user-actionable messages (never raw RPC codes).

Telethon is imported lazily so this module (and every interface built on it)
imports cleanly even when Telethon is not installed.
"""
import asyncio
import inspect
import logging
from typing import Any, Optional, Tuple, Type

from .exceptions import AuthError, ConnectionStateError, RateLimitExceeded, TeloudeTelegramError

logger = logging.getLogger("TelegramBridge")

_FLOOD_ERRORS: Optional[Tuple[Type[BaseException], ...]] = None


def _flood_error_types() -> Tuple[Type[BaseException], ...]:
    global _FLOOD_ERRORS
    if _FLOOD_ERRORS is None:
        try:
            from telethon.errors import FloodWaitError

            _FLOOD_ERRORS = (FloodWaitError,)
        except ImportError:
            _FLOOD_ERRORS = ()
    return _FLOOD_ERRORS


def run_sync(value: Any) -> Any:
    """Runs awaitables to completion; passes plain values through.

    Raises ConnectionStateError when called from inside an already-running
    event loop: the sync foundation cannot block on it. A coherent async
    architecture arrives with the transfer engine in a later phase.
    """
    if inspect.isawaitable(value):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)
        if inspect.iscoroutine(value):
            value.close()  # avoid "never awaited" warnings; we deliberately refuse it
        raise ConnectionStateError(
            "Cannot complete the Telegram operation from inside a running "
            "event loop with the sync client foundation."
        )
    return value


def map_rpc_error(exc: BaseException, operation: str = "Telegram operation") -> TeloudeTelegramError:
    """Maps a low-level failure to a Teloude exception (returned, not raised)."""
    flood_types = _flood_error_types()
    if flood_types and isinstance(exc, flood_types):
        seconds = int(getattr(exc, "seconds", 60) or 60)
        return RateLimitExceeded(
            f"Telegram temporarily limited this operation ({operation}). "
            "Teloude will retry automatically.",
            retry_after=seconds,
            details=str(exc),
        )
    if isinstance(exc, TeloudeTelegramError):
        return exc
    if isinstance(exc, OSError):
        return ConnectionStateError(
            f"Could not reach Telegram during {operation}. "
            "Check the internet connection; Teloude will retry automatically.",
            details=str(exc),
        )
    return TeloudeTelegramError(f"{operation} failed.", details=str(exc) or repr(exc))


def extract_chat_id(updates: Any) -> Optional[int]:
    """Finds the first chat/channel id in a Telethon Updates-like response."""
    for chat in getattr(updates, "chats", []) or []:
        chat_id = getattr(chat, "id", None)
        if chat_id is not None:
            return int(chat_id)
    return None


def extract_message_id(updates: Any) -> Optional[int]:
    """Finds the first new-message id in a Telethon Updates-like response."""
    for update in getattr(updates, "updates", []) or []:
        message = getattr(update, "message", None)
        msg_id = getattr(message, "id", None) if message is not None else None
        if msg_id is not None:
            return int(msg_id)
    return None
