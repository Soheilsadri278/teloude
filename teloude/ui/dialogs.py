# teloude/ui/dialogs.py
"""Blocking question dialogs callable from worker threads.

Engines resolve duplicates/collisions synchronously on worker threads, but
Qt widgets must live on the UI thread. ask_on_ui() marshals a question to a
UI-thread handler and blocks the worker until the user answers. Calling it
from the UI thread raises (would deadlock).
"""
from dataclasses import dataclass
from typing import Any, Generic, Optional, TypeVar

from PySide6 import QtCore, QtWidgets

from teloude.ui.components import present_blocking

T = TypeVar("T")


@dataclass
class Question(Generic[T]):
    title: str
    text: str
    options: list
    check_text: Optional[str] = None


@dataclass
class Answer(Generic[T]):
    choice: Any
    checked: bool = False


class UiThreadAsker(QtCore.QObject):
    """Lives on the UI thread; worker threads post questions through it."""

    request = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.request.connect(self._handle, QtCore.Qt.BlockingQueuedConnection)

    def ask(self, question: Question) -> Answer:
        if QtCore.QThread.currentThread() == self.thread():
            raise RuntimeError("ask_on_ui() must not be called from the UI thread.")
        box: list = []
        self.request.emit((question, box))
        return box[0]

    def _handle(self, payload) -> None:
        question, box = payload
        dialog = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Question,
            question.title,
            question.text,
            QtWidgets.QMessageBox.StandardButton.NoButton,
            None,
        )
        buttons = {}
        for label, value in question.options:
            button = dialog.addButton(label, QtWidgets.QMessageBox.ButtonRole.ActionRole)
            buttons[button] = value
        check = None
        if question.check_text:
            check = QtWidgets.QCheckBox(question.check_text)
            dialog.setCheckBox(check)
        # Same blocking question as before, now with the app dimmed behind it.
        present_blocking(dialog, QtWidgets.QApplication.activeWindow())
        clicked = dialog.clickedButton()
        box.append(Answer(choice=buttons.get(clicked), checked=bool(check and check.isChecked())))


def make_collision_callback(asker):
    """Builds an engine collision_callback that asks on the UI thread."""
    from teloude.core.restore import CollisionAction, CollisionDecision

    def ask_collision(record, target, index, total):
        answer: Answer = asker.ask(Question(
            title="File already exists",
            text=f"'{target}' already exists ({index}/{total}).",
            options=[("Skip", CollisionAction.SKIP),
                     ("Overwrite", CollisionAction.OVERWRITE),
                     ("Keep both", CollisionAction.KEEP_BOTH),
                     ("Cancel restore", CollisionAction.CANCEL)],
            check_text="Apply to all",
        ))
        choice = (answer.choice if isinstance(answer.choice, CollisionAction)
                  else CollisionAction.SKIP)
        return CollisionDecision(choice, answer.checked)

    return ask_collision


def _message_box(parent, icon, title: str, message: str, buttons, default=None):
    """A themed, scrim-backed message box.

    Built explicitly instead of using the static `QMessageBox.warning(...)` style
    helpers, because those run their own event loop and give no chance to dim the
    window behind them. Behaviour is identical - same icon, same buttons, same
    modality - and the return value is the same standard button.
    """
    box = QtWidgets.QMessageBox(icon, title, message, buttons, parent)
    if default is not None:
        box.setDefaultButton(default)
    present_blocking(box, parent)
    return box.standardButton(box.clickedButton())


def confirm_destructive(parent, title: str, text: str, confirm_label: str) -> bool:
    """Modal destructive-action confirmation (call from the UI thread)."""
    answer = _message_box(
        parent, QtWidgets.QMessageBox.Icon.Warning, title, text,
        QtWidgets.QMessageBox.StandardButton.Cancel | QtWidgets.QMessageBox.StandardButton.Yes,
        QtWidgets.QMessageBox.StandardButton.Cancel,
    )
    _ = confirm_label  # label kept for API clarity; Qt shows Yes/Cancel
    return answer == QtWidgets.QMessageBox.StandardButton.Yes


def show_error(parent, title: str, message: str) -> None:
    _message_box(parent, QtWidgets.QMessageBox.Icon.Critical, title, message,
                 QtWidgets.QMessageBox.StandardButton.Ok,
                 QtWidgets.QMessageBox.StandardButton.Ok)


def show_info(parent, title: str, message: str) -> None:
    _message_box(parent, QtWidgets.QMessageBox.Icon.Information, title, message,
                 QtWidgets.QMessageBox.StandardButton.Ok,
                 QtWidgets.QMessageBox.StandardButton.Ok)
