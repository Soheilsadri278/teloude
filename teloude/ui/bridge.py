# teloude/ui/bridge.py
"""Qt bridge for application-service events.

ServiceBus callbacks fire on worker threads. This QObject lives on the UI
thread and re-emits them as Qt signals, so connected slots always execute on
the UI thread (Qt queues cross-thread emissions automatically).
"""
from typing import Any

from PySide6 import QtCore

from teloude.application.services import EventBus

FORWARDED_EVENTS = (
    "auth_state",
    "connection_state",
    "storages_changed",
    "backup_planned",
    "backup_progress",
    "backup_done",
    "restore_progress",
    "restore_done",
    "transfer_state",
)


class ServiceBridge(QtCore.QObject):
    auth_state = QtCore.Signal(dict)
    connection_state = QtCore.Signal(dict)
    storages_changed = QtCore.Signal(dict)
    backup_planned = QtCore.Signal(dict)
    backup_progress = QtCore.Signal(dict)
    backup_done = QtCore.Signal(dict)
    restore_progress = QtCore.Signal(dict)
    restore_done = QtCore.Signal(dict)
    transfer_state = QtCore.Signal(dict)

    def __init__(self, bus: EventBus, parent=None):
        super().__init__(parent)
        self._bus = bus
        self._subscriptions = [
            (event, bus.subscribe(event, self._make_forwarder(event)))
            for event in FORWARDED_EVENTS
        ]

    def detach(self) -> None:
        """Stops forwarding application events to the UI (idempotent).

        Called when the window is torn down: a service that finishes after
        shutdown would otherwise still post an update to a page that is already
        gone. Late deliveries like that are what makes a teardown unpredictable
        (Qt keeps the queued signal and hands it to whatever runs next), so the
        bridge unhooks itself from the bus before the widgets disappear.
        """
        subscriptions, self._subscriptions = self._subscriptions, []
        for event, callback in subscriptions:
            self._bus.unsubscribe(event, callback)

    def _make_forwarder(self, event: str):
        signal = getattr(self, event)

        def forward(payload: Any) -> None:
            signal.emit(dict(payload or {}))

        return forward
