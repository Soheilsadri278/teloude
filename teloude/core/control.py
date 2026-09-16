# teloude/core/control.py
"""Cooperative pause/cancel control shared by the backup and restore engines.

Engines poll should_pause()/is_cancelled() between units of work; gateways
raise UploadPaused/UploadCancelled between parts. wait_if_paused() blocks the
worker (never the UI thread) until resume() or cancel().

The control also owns the *reported* state of a run, so the UI never has to
guess (spec sections 17-18: pause/resume must be visible and persisted):

    RUNNING ---- pause() ----> PAUSING ---- worker parks ----> PAUSED
       ^                          |                             |
       |                          +-------- resume() ----------+
       |                                                        |
       +-- transfer moves again ----- RESUMING <-- resume() ----+

ControlState describes a *running* run; RunOutcome describes one that stopped.
Both travel to the UI as plain strings, so no widget ever has to infer what is
happening from progress counters alone.

A transition only becomes real when the worker confirms it: PAUSED is entered
inside wait_if_paused() (the worker is genuinely parked) and RUNNING is restored
when the transfer demonstrably moves again (should_pause()/check_cancelled() are
polled by the working loops). Repeated commands are ignored while a transition
is in flight, so double-clicking Pause (or Resume) cannot leave the UI lying
about the transfer state.

The module is UI-agnostic: observers are plain callables, so Qt never leaks into
the core and the engines stay testable without a display.
"""
import threading
import time
from enum import Enum
from typing import Callable, Optional


class EngineCancelled(Exception):
    """Raised to abort a whole backup/restore run after cancel()."""


class ControlState(str, Enum):
    """Actual state of a transfer run, as observed by the worker."""

    RUNNING = "running"      # uploading / restoring / hashing
    PAUSING = "pausing"      # pause requested, worker still finishing a unit
    PAUSED = "paused"        # worker is parked; nothing is being transferred
    RESUMING = "resuming"    # resume requested, worker starting again
    CANCELLING = "cancelling"  # cancel requested, run is stopping


class RunOutcome(str, Enum):
    """How a run ended, reported by the services when it stops."""

    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EngineControl:
    def __init__(
        self,
        sleeper: Callable[[float], None] = time.sleep,
        on_state_change: Optional[Callable[[ControlState], None]] = None,
    ):
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._sleeper = sleeper
        self._state = ControlState.RUNNING
        self._state_lock = threading.Lock()
        self._on_state_change = on_state_change

    # -- state ----------------------------------------------------------
    @property
    def state(self) -> ControlState:
        with self._state_lock:
            return self._state

    def set_state_listener(self, listener: Optional[Callable[[ControlState], None]]) -> None:
        """Registers the observer called on every real state change."""
        self._on_state_change = listener

    def _set_state(self, state: ControlState) -> None:
        """Records a transition and tells the observer; no-op when unchanged."""
        with self._state_lock:
            if self._state is state:
                return
            self._state = state
        listener = self._on_state_change
        if listener is not None:
            try:
                listener(state)
            except Exception:  # a broken observer must not stop a transfer
                pass

    # -- commands (called from the UI thread) ---------------------------
    def pause(self) -> None:
        """Requests a pause; ignored while a pause is pending, parked or cancelled."""
        with self._state_lock:
            if self._state in (ControlState.PAUSING, ControlState.PAUSED,
                               ControlState.CANCELLING):
                return
        self._pause.set()
        self._set_state(ControlState.PAUSING)

    def resume(self) -> None:
        """Requests a resume; ignored while running, resuming or cancelled.

        Resuming a pending pause is allowed: it undoes the request before the
        worker parks, so a mistaken Pause never traps the user in PAUSING.
        """
        with self._state_lock:
            if self._state in (ControlState.RUNNING, ControlState.RESUMING,
                               ControlState.CANCELLING):
                return
        self._pause.clear()
        self._set_state(ControlState.RESUMING)

    def cancel(self) -> None:
        self._cancel.set()
        self._set_state(ControlState.CANCELLING)
        self._pause.clear()  # unblock parkers so they observe cancellation

    # -- observations (called from any thread) --------------------------
    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def should_pause(self) -> bool:
        """Polled by the transfer loops; also proves the transfer is moving."""
        if not self._pause.is_set():
            self._note_moving()
        return self._pause.is_set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise EngineCancelled("Operation cancelled.")
        if not self._pause.is_set():
            self._note_moving()

    def wait_if_paused(self) -> None:
        """Parks the worker until resume()/cancel(); reports the real states."""
        was_paused = False
        if self._pause.is_set() and not self._cancel.is_set():
            # The worker has reached a safe point: this is the moment the pause
            # becomes real, not when the user clicked the button.
            was_paused = True
            self._set_state(ControlState.PAUSED)
        while self._pause.is_set() and not self._cancel.is_set():
            was_paused = True
            self._sleeper(0.05)
        if self._cancel.is_set():
            raise EngineCancelled("Operation cancelled.")
        if was_paused:
            # Leaving the park: the transfer is starting again. RUNNING follows
            # as soon as real work happens (see _note_moving); parking itself is
            # a safe point, not proof that the transfer moved.
            self._set_state(ControlState.RESUMING)

    def _note_moving(self) -> None:
        """Confirms RUNNING once the transfer demonstrably continues."""
        with self._state_lock:
            if self._state not in (ControlState.PAUSING, ControlState.RESUMING):
                return
        self._set_state(ControlState.RUNNING)
