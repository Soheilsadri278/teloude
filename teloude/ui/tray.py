# teloude/ui/tray.py
"""Windows system tray icon with Open / Pause / Resume / Progress / Exit."""
from pathlib import Path
import sys
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets


def _icon_search_bases() -> list:
    """Where ``assets/`` can live: the frozen bundle, or the repository root."""
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    bases.append(Path(__file__).resolve().parents[2])
    return bases


def _default_icon() -> QtGui.QIcon:
    """The official Teloude icon; a painted mark only if the asset is gone.

    The tray carries the same icon as the window and the taskbar - the
    application's assets, not a look-alike. The painted fallback exists so a
    bare source checkout without ``assets/`` still shows something honest
    rather than an empty tray slot.
    """
    icon = QtGui.QIcon()
    for base in _icon_search_bases():
        for name in ("icon.ico", "icon.png"):
            candidate = base / "assets" / name
            if candidate.is_file():
                icon.addFile(str(candidate))
        if not icon.isNull():
            return icon
    pixmap = QtGui.QPixmap(32, 32)
    pixmap.fill(QtGui.QColor("#1f6feb"))
    painter = QtGui.QPainter(pixmap)
    painter.setPen(QtGui.QColor("white"))
    painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()
    return QtGui.QIcon(pixmap)


class TrayController(QtCore.QObject):
    """Owns the QSystemTrayIcon; Exit performs a safe shutdown via callback."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_pause_all: Callable[[], None],
        on_resume_all: Callable[[], None],
        on_progress: Callable[[], None],
        on_exit: Callable[[], None],
        parent=None,
    ):
        super().__init__(parent)
        self._tray = QtWidgets.QSystemTrayIcon(_default_icon(), parent)
        self._tray.setToolTip("Teloude Backup")
        menu = QtWidgets.QMenu()
        open_action = menu.addAction("Open Teloude")
        open_action.triggered.connect(lambda _checked=False: on_open())
        pause_action = menu.addAction("Pause Backup")
        pause_action.triggered.connect(lambda _checked=False: on_pause_all())
        resume_action = menu.addAction("Resume Backup")
        resume_action.triggered.connect(lambda _checked=False: on_resume_all())
        progress_action = menu.addAction("View Progress")
        progress_action.triggered.connect(lambda _checked=False: on_progress())
        menu.addSeparator()
        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(lambda _checked=False: on_exit())
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda reason: on_open()
            if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger
            else None
        )

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def show_message(self, title: str, message: str, kind: str = "info") -> None:
        """Balloon notification; ``kind`` picks the OS icon (info/error)."""
        icon = QtWidgets.QSystemTrayIcon.MessageIcon.Information
        if kind == "error":
            icon = QtWidgets.QSystemTrayIcon.MessageIcon.Critical
        self._tray.showMessage(title, message, icon)

    @property
    def is_supported(self) -> bool:
        return self._tray.isSystemTrayAvailable()
