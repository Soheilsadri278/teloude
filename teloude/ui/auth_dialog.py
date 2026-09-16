# teloude/ui/auth_dialog.py
"""Telegram sign-in wizard: phone -> login code -> 2FA password (if needed)."""
import logging

from PySide6 import QtWidgets

from teloude.application.services import AuthService
from teloude.infrastructure.telegram.auth import AuthState
from teloude.ui import theme
from teloude.ui.components import GlassCard, fade_in, scrim_for
from teloude.ui.workers import run_in_background

logger = logging.getLogger("AuthDialog")


class AuthDialog(QtWidgets.QDialog):
    """Modal first-run sign-in. Returns QDialog.Accepted once authorized."""

    def __init__(self, auth_service: AuthService, parent=None):
        super().__init__(parent)
        self._auth = auth_service
        self._phone = ""
        self.setWindowTitle("Connect to Telegram")
        self.setModal(True)
        self.setMinimumWidth(380)
        layout = QtWidgets.QVBoxLayout(self)

        # One glass card holds the wizard: the surface treatment of a dialog in
        # this design system (opaque fill, hairline, inner highlight, spring
        # entry). The window itself stays a normal native window.
        self.card = GlassCard()
        layout.addWidget(self.card)
        card_layout = QtWidgets.QVBoxLayout(self.card)
        card_layout.setContentsMargins(*([theme.SPACING["md"]] * 4))
        card_layout.setSpacing(theme.SPACING["sm"])
        self._card_layout = card_layout

        self.stack = QtWidgets.QStackedWidget()
        card_layout.addWidget(self.stack)

        # Page 0: phone
        phone_page = QtWidgets.QWidget()
        phone_layout = QtWidgets.QFormLayout(phone_page)
        self.phone_edit = QtWidgets.QLineEdit("+")
        self.phone_edit.setPlaceholderText("+1234567890")
        phone_layout.addRow("Phone number:", self.phone_edit)
        self.send_button = QtWidgets.QPushButton("Send login code")
        self.send_button.clicked.connect(self._on_send_code)
        phone_layout.addRow(self.send_button)
        self.stack.addWidget(phone_page)

        # Page 1: code
        code_page = QtWidgets.QWidget()
        code_layout = QtWidgets.QFormLayout(code_page)
        self.code_edit = QtWidgets.QLineEdit()
        self.code_edit.setPlaceholderText("Code from Telegram")
        self.code_edit.setMaxLength(32)
        code_layout.addRow("Login code:", self.code_edit)
        self.code_button = QtWidgets.QPushButton("Sign in")
        self.code_button.clicked.connect(self._on_submit_code)
        code_layout.addRow(self.code_button)
        self.stack.addWidget(code_page)

        # Page 2: 2FA password
        password_page = QtWidgets.QWidget()
        password_layout = QtWidgets.QFormLayout(password_page)
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        password_layout.addRow("2FA password:", self.password_edit)
        self.password_button = QtWidgets.QPushButton("Confirm")
        self.password_button.clicked.connect(self._on_submit_password)
        password_layout.addRow(self.password_button)
        self.stack.addWidget(password_page)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        card_layout.addWidget(self.status_label)

        self._scrim = None
        theme.normalize_layout_spacing(self)
        fade_in(self.card, lift_px=theme.SPACING["xs"], layout=layout)

    # -- the app dims while the sign-in wizard is up (parented dialogs only) --
    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.parent().window() if isinstance(self.parent(), QtWidgets.QWidget) else None
        if isinstance(window, QtWidgets.QMainWindow) and window is not self:
            self._scrim = scrim_for(window)
            self._scrim.push()

    def done(self, result: int) -> None:
        if self._scrim is not None:
            self._scrim.pop()
            self._scrim = None
        super().done(result)

    def _busy(self, busy: bool, message: str = "") -> None:
        for widget in (self.send_button, self.code_button, self.password_button):
            widget.setEnabled(not busy)
        self.status_label.setText(message)

    def _on_send_code(self) -> None:
        self._phone = self.phone_edit.text().strip()
        if not self._phone:
            self.status_label.setText("Enter your phone number in international format.")
            return
        self._busy(True, "Requesting a login code from Telegram...")
        run_in_background(
            lambda: self._auth.start_login(self._phone),
            on_done=self._after_send_code,
            on_error=self._on_error,
        )

    def _after_send_code(self, state: AuthState) -> None:
        self._busy(False)
        if state is AuthState.CODE_SENT:
            self.stack.setCurrentIndex(1)
            self.code_edit.setFocus()
            self.status_label.setText("Enter the code Telegram just sent you.")
        else:
            self.status_label.setText(f"Unexpected state: {state.value}")

    def _on_submit_code(self) -> None:
        code = self.code_edit.text().strip()
        if not code:
            self.status_label.setText("Enter the login code.")
            return
        self._busy(True, "Signing in...")
        phone = self._phone
        run_in_background(
            lambda: self._auth.submit_code(phone, code),
            on_done=self._after_code,
            on_error=self._on_error,
        )

    def _after_code(self, state: AuthState) -> None:
        self._busy(False)
        if state is AuthState.AUTHORIZED:
            self.accept()
        elif state is AuthState.PASSWORD_NEEDED:
            self.stack.setCurrentIndex(2)
            self.password_edit.setFocus()
            self.status_label.setText("Two-step verification is enabled. Enter your password.")
        else:
            self.status_label.setText(f"Unexpected state: {state.value}")

    def _on_submit_password(self) -> None:
        password = self.password_edit.text()
        if not password:
            self.status_label.setText("Enter your 2FA password.")
            return
        self._busy(True, "Verifying...")
        # Never log the password; the service layer never logs secrets either.
        run_in_background(
            lambda: self._auth.submit_password(password),
            on_done=lambda state: self.accept() if state is AuthState.AUTHORIZED else None,
            on_error=self._on_error,
        )
        self.password_edit.clear()

    def _on_error(self, message: str) -> None:
        self._busy(False)
        if "incorrect" in message.lower() or "expired" in message.lower():
            self.status_label.setText(message)
        else:
            logger.warning(f"Sign-in step failed: {type(message).__name__}")
            self.status_label.setText(message)
