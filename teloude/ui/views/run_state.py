# teloude/ui/views/run_state.py
"""How a run's real control state is shown, shared by Backup and Restore (Bug 3).

The views never invent a state: they render exactly what the control reported
(`transfer_state` events, or a direct query of the running service) and they
only offer commands that can actually change something. Repeat Pause while the
worker is still finishing a unit, or Resume while it already runs, is not
offered at all - clicking it would either be ignored or look like a lie.

The rules live here, away from the widgets, so both pages behave identically and
the mapping stays unit-testable without a display.
"""
from typing import Optional, Tuple

from teloude.core.control import ControlState, RunOutcome

# A run was accepted by the service but has not reported itself yet. This is a
# statement about the *command*, not about the transfer, so no control is
# offered until the engine says what it is really doing.
STARTING = "starting"

# Controls that exist while a run is in flight (Cancel needs a live run).
_ACTIVE_STATES = (
    ControlState.RUNNING.value,
    ControlState.PAUSING.value,
    ControlState.PAUSED.value,
    ControlState.RESUMING.value,
)

# Which verb a kind of run uses while it is simply working.
_KIND_WORKING = {"backup": "Uploading", "restore": "Restoring"}

_SHARED_LABELS = {
    STARTING: "Starting\u2026",
    ControlState.PAUSING.value: "Pausing\u2026",
    ControlState.PAUSED.value: "Paused",
    ControlState.RESUMING.value: "Resuming\u2026",
    ControlState.CANCELLING.value: "Cancelling\u2026",
    RunOutcome.COMPLETED.value: "Completed",
    RunOutcome.COMPLETED_WITH_ERRORS.value: "Completed with errors",
    RunOutcome.FAILED.value: "Failed",
    RunOutcome.CANCELLED.value: "Cancelled",
}

# Outcomes mean the run is over: controls are gone, the start buttons return.
FINISHED_STATES = (
    RunOutcome.COMPLETED.value,
    RunOutcome.COMPLETED_WITH_ERRORS.value,
    RunOutcome.FAILED.value,
    RunOutcome.CANCELLED.value,
)


def state_label(kind: str, state: Optional[str]) -> str:
    """Human label for a reported state; empty string when nothing is known."""
    if not state:
        return ""
    if state == ControlState.RUNNING.value:
        return _KIND_WORKING.get(kind, "Working")
    return _SHARED_LABELS.get(state, "")


def is_active(state: Optional[str]) -> bool:
    """True while a run is in flight (any state before a terminal outcome)."""
    return state in _ACTIVE_STATES


def controls_for(state: Optional[str]) -> Tuple[bool, bool, bool]:
    """(pause, resume, cancel) availability for a reported state.

    Pause is offered only while the transfer really runs, and Resume only while
    a pause is pending or in effect. A second Pause during "Pausing..." - or a
    second Resume during "Resuming..." - is therefore not offered at all.
    Resume stays available while the pause is still pending, so a mistaken
    Pause can always be undone instead of trapping the user in the transition.
    """
    return (
        state == ControlState.RUNNING.value,
        state in (ControlState.PAUSING.value, ControlState.PAUSED.value),
        is_active(state),
    )


def can_start(state: Optional[str]) -> bool:
    """True when no run is in flight (nothing started, or the last one ended)."""
    return state != STARTING and not is_active(state)
