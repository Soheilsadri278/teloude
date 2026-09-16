# teloude/ui/workers.py
"""Background task runner: keeps long work off the Qt UI thread."""
import logging
import traceback
from typing import Any, Callable, Optional

from PySide6 import QtCore

logger = logging.getLogger("UiWorkers")


class _Signals(QtCore.QObject):
    """Result carriers that own their own lifetime bookkeeping.

    The clean-up slot lives here (not on the runnable): QThreadPool deletes the
    runnable right after run(), so a slot on that object would touch freed
    memory, while this object stays referenced until its result is delivered.
    """

    done = QtCore.Signal(object)
    error = QtCore.Signal(str)

    def __init__(self):
        super().__init__()

    def arm_release(self) -> None:
        """Queues the clean-up after the caller's own slots.

        Order matters: releasing while the caller's queued callback is still
        pending would destroy the carrier before that callback arrives.
        """
        self.done.connect(self._release)
        self.error.connect(self._release)

    def _release(self, _payload: Any) -> None:
        _ACTIVE.discard(self)


# Queue listeners keep each task's signal object alive until its result has been
# delivered. QThreadPool destroys the runnable as soon as run() returns; without
# this reference Qt would drop the still-queued signal, and every callback the
# caller passed (button re-enable, refresh, dialog step) would silently never
# run. Views call this helper without keeping the returned task, so the reference
# has to live here.
_ACTIVE: "set[QtCore.QObject]" = set()


class BackgroundTask(QtCore.QRunnable):
    """Runs fn() on the global QThreadPool; delivers results via signals."""

    def __init__(
        self,
        fn: Callable[[], Any],
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self._fn = fn
        self.signals = _Signals()
        if on_done is not None:
            self.signals.done.connect(on_done)
        if on_error is not None:
            self.signals.error.connect(on_error)
        self.signals.arm_release()

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            logger.warning(f"Background task failed: {exc}\n{traceback.format_exc()}")
            self.signals.error.emit(str(exc) or type(exc).__name__)
        else:
            self.signals.done.emit(result)


def run_in_background(
    fn: Callable[[], Any],
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> BackgroundTask:
    """Starts fn() off the UI thread; callbacks run on the UI thread.

    The returned task does not need to be kept: results are delivered either way.
    """
    task = BackgroundTask(fn, on_done, on_error)
    _ACTIVE.add(task.signals)
    QtCore.QThreadPool.globalInstance().start(task)
    return task


def pending_tasks() -> int:
    """Number of results still waiting to be delivered (test/diagnostic helper)."""
    return len(_ACTIVE)
