# teloude/ui/notifications.py
"""Completion notifications for backup and restore runs.

Lifecycle, not timers: the notifier subscribes to the same application event
bus the services already publish (``backup_done`` / ``restore_done``), so a
notification is raised exactly once per finished run, whether or not the main
window is visible or the app sits in the tray. Nothing here polls and no
widget is involved.

Deduplication is by run delivery, never by content: the exact payload object
a service published is remembered, so a replay of the same event (a double
delivery, a late UI replay of history) is ignored, while a genuine second run
- whose payload is a fresh dictionary, even when every counter matches the
previous run - notifies again.

Display is delegated to a ``SystemNotifier`` wrapper around the existing
``TrayController`` - the one OS surface that works with the window closed to
the tray. When no tray is available (or in headless tests) the notification
degrades to the log, never to a modal.

This module is deliberately Qt-free: the bus is plain Python, so the logic is
unit-testable without a display.
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Notifications")


def failure_summary(failed: List[Any], limit: int = 3) -> str:
    """The first failed paths as one line (``a.txt, b/c.bin`` + …and N more).

    Rows are ``(relative_path, message)`` tuples from the engines' reports;
    anything that does not parse is skipped rather than crashing a
    notification, and only parseable rows are counted.
    """
    names: List[str] = []
    for row in failed or []:
        try:
            path = row[0]
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(path, str) and path:
            names.append(path)
    if not names:
        return ""
    summary = ", ".join(names[:limit])
    extra = len(names) - limit
    if extra > 0:
        summary = f"{summary} …and {extra} more"
    return summary


def completion_payload(payload: Dict[str, Any], verb: Optional[str] = None) -> Dict[str, Any]:
    """Turns a service payload into (severity, title, body) or ``{}``.

    ``verb`` names the operation ("backup"/"restore"); a payload that carries
    its own "verb" key works too. Success only for a genuinely clean run: a
    cancelled run is the user's own action (no notification, per spec), and
    any failure entry - or the services' ``error`` flag from the hard-failure
    path - becomes an error notification, never a success one.
    """
    if not isinstance(payload, dict) or payload.get("cancelled"):
        return {}
    verb = verb or payload.get("verb") or "operation"
    if payload.get("error") or payload.get("failed"):
        title, body = {
            "backup": ("Backup failed", "The backup could not be finished."),
            "restore": ("Restore failed", "The restore could not be finished."),
        }.get(verb, ("Operation failed", "The operation could not be finished."))
        summary = failure_summary(payload.get("failed") or [])
        if summary:
            body = f"{body} ({summary})"
        return {"severity": "error", "title": title, "body": body}
    if verb == "backup":
        return {
            "severity": "info",
            "title": "Backup completed",
            "body": "Your backup has finished successfully.",
        }
    if verb == "restore":
        return {
            "severity": "info",
            "title": "Restore completed",
            "body": "Your restore has finished successfully.",
        }
    return {}


class SystemNotifier:
    """The OS surface: the existing tray icon's showMessage, guarded."""

    def __init__(self, tray: Any = None):
        self._tray = tray

    @property
    def available(self) -> bool:
        supported = getattr(self._tray, "is_supported", False)
        try:
            supported = bool(supported) if not callable(supported) else bool(supported())
        except Exception:
            supported = False
        return bool(self._tray is not None and supported)

    def show(self, kind: str, title: str, body: str) -> None:
        """Kind is ``info`` or ``error``; degrade to the log when no tray."""
        if not self.available:
            logger.info("%s: %s", title, body)
            return
        try:
            self._tray.show_message(title, body, kind)
        except TypeError:
            # A tray controller with the classic (title, body) signature.
            self._tray.show_message(title, body)
        except Exception as exc:
            logger.warning("Could not show the notification: %s", exc)


class OperationNotifier:
    """Subscribes completion events; one notification per finished run."""

    MAX_REMEMBERED = 64

    def __init__(self, bus, notifier: Callable[[str, str, str], None]):
        self._bus = bus
        self._notifier = notifier
        # payload id -> (delivering thread id, the payload itself). Holding the
        # payload keeps its id unique and stable for as long as it is
        # remembered, so a fresh run's dictionary can never collide with a
        # collected one.
        self._seen: Dict[int, tuple] = {}
        self._closed = False
        bus.subscribe("backup_done", self._on_backup_done)
        bus.subscribe("restore_done", self._on_restore_done)

    # -- wiring --------------------------------------------------------------
    def close(self) -> None:
        """Unsubscribes (application shutdown); the notifier idles afterwards."""
        if self._closed:
            return
        self._closed = True
        self._bus.unsubscribe("backup_done", self._on_backup_done)
        self._bus.unsubscribe("restore_done", self._on_restore_done)

    # -- events --------------------------------------------------------------
    def _on_backup_done(self, payload: Any) -> None:
        self._handle("backup", payload)

    def _on_restore_done(self, payload: Any) -> None:
        self._handle("restore", payload)

    def _handle(self, verb: str, payload: Any) -> None:
        if self._closed or not isinstance(payload, dict):
            return
        key = id(payload)
        delivering = id(threading.current_thread())
        if key in self._seen:
            return  # the same event delivered twice: never notify twice
        if len(self._seen) >= self.MAX_REMEMBERED:
            for stale in list(self._seen)[:-self.MAX_REMEMBERED // 2]:
                self._seen.pop(stale, None)
        self._seen[key] = (delivering, payload)
        message = completion_payload(payload, verb)
        if not message:
            return  # cancelled runs are the user's own action: stay quiet
        try:
            self._notifier(
                message["severity"], message["title"], message["body"]
            )
        except Exception:
            logger.exception("Notification delivery failed.")


def install(bus, tray: Any) -> OperationNotifier:
    """Creates the notifier on the application bus with the tray as surface."""
    return OperationNotifier(bus, SystemNotifier(tray).show)
