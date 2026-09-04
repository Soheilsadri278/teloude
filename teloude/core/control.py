# teloude/core/control.py
"""Cooperative pause/cancel control shared by the backup and restore engines.

Engines poll should_pause()/is_cancelled() between units of work; gateways
raise UploadPaused/UploadCancelled between parts. wait_if_paused() blocks the
worker (never the UI thread) until resume() or cancel().
"""
import threading
import time
from typing import Callable


class EngineCancelled(Exception):
    """Raised to abort a whole backup/restore run after cancel()."""


class EngineControl:
    def __init__(self, sleeper: Callable[[float], None] = time.sleep):
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._sleeper = sleeper

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()  # unblock waiters so they observe cancellation

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def should_pause(self) -> bool:
        return self._pause.is_set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise EngineCancelled("Operation cancelled.")

    def wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._cancel.is_set():
            self._sleeper(0.05)
        self.check_cancelled()
