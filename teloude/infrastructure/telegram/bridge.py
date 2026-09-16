# teloude/infrastructure/telegram/bridge.py
"""Shared plumbing for the Telegram infrastructure layer (no UI/core imports).

- run_sync: drives Telethon's coroutines from synchronous foundation code on
  ONE dedicated daemon event-loop thread ("teloude-telegram-loop"), so the
  Telethon client always lives on the same loop no matter which worker thread
  calls it. Calling run_sync from the loop thread itself raises.
- map_rpc_error: translates Telethon RPC failures into Teloude's exception
  hierarchy with user-actionable messages (never raw RPC codes).

Telethon is imported lazily so this module (and every interface built on it)
imports cleanly even when Telethon is not installed.
"""
import asyncio
import inspect
import logging
import threading
from typing import Any, Optional, Tuple, Type

from .exceptions import ConnectionStateError, RateLimitExceeded, TeloudeTelegramError

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


class _TelegramLoopThread(threading.Thread):
    """Single owner of the asyncio loop all Telethon coroutines run on."""

    def __init__(self) -> None:
        super().__init__(name="teloude-telegram-loop", daemon=True)
        self._ready = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        if not self.is_alive():
            self.start()
        if not self._ready.wait(timeout=15) or self.loop is None:
            raise ConnectionStateError("Could not start the Telegram network thread.")
        return self.loop


_loop_guard = threading.Lock()
_loop_thread: Optional[_TelegramLoopThread] = None


def get_telegram_loop() -> asyncio.AbstractEventLoop:
    """Returns the shared Telegram event loop, starting its thread on demand."""
    global _loop_thread
    with _loop_guard:
        if _loop_thread is None:
            _loop_thread = _TelegramLoopThread()
        thread = _loop_thread
    return thread.ensure_loop()


async def _await_value(value: Any) -> Any:
    return await value


def run_sync(value: Any) -> Any:
    """Runs awaitables on the shared Telegram loop; passes values through.

    Safe to call from any thread (including threads running an unrelated
    event loop) because the awaitable executes on the dedicated loop thread.
    Raises ConnectionStateError when called from the Telegram loop thread
    itself, where blocking would deadlock. Coroutine failures propagate to
    the caller unchanged for the usual error mapping upstream.
    """
    if not inspect.isawaitable(value):
        return value
    current = threading.current_thread()
    with _loop_guard:
        ours = _loop_thread
    if ours is not None and current is ours:
        if inspect.iscoroutine(value):
            value.close()  # avoid "never awaited" warnings; we deliberately refuse it
        raise ConnectionStateError(
            "Cannot wait on a Telegram operation from the Telegram "
            "network thread itself."
        )
    loop = get_telegram_loop()
    if not inspect.iscoroutine(value):
        async def _wrap() -> Any:
            return await value

        coro = _wrap()
    else:
        coro = value
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


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
