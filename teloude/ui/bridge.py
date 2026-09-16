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
    storages_changed = QtCore.Signal(dict)
    backup_planned = QtCore.Signal(dict)
    backup_progress = QtCore.Signal(dict)
    backup_done = QtCore.Signal(dict)
    restore_progress = QtCore.Signal(dict)
    restore_done = QtCore.Signal(dict)
    transfer_state = QtCore.Signal(dict)

    def __init__(self, bus: EventBus, parent=None):
        super().__init__(parent)
        for event in FORWARDED_EVENTS:
            bus.subscribe(event, self._make_forwarder(event))

    def _make_forwarder(self, event: str):
        signal = getattr(self, event)

        def forward(payload: Any) -> None:
            signal.emit(dict(payload or {}))

        return forward
