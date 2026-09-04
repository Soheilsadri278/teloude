# teloude/ui/workers.py
"""Background task runner: keeps long work off the Qt UI thread."""
import traceback
from typing import Any, Callable, Optional

from PySide6 import QtCore


class _Signals(QtCore.QObject):
    done = QtCore.Signal(object)
    error = QtCore.Signal(str)


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

    def run(self) -> None:
        try:
            self.signals.done.emit(self._fn())
        except Exception as exc:
            traceback.print_exc()
            self.signals.error.emit(str(exc) or type(exc).__name__)


def run_in_background(
    fn: Callable[[], Any],
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> BackgroundTask:
    task = BackgroundTask(fn, on_done, on_error)
    QtCore.QThreadPool.globalInstance().start(task)
    return task
