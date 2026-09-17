# teloude/ui/proxy_dialog.py
"""Connection settings: the proxy page behind the connection icon.

Telegram's arrangement, not a form bolted onto the login screen: the sign-in
wizard stays phone -> code -> password, and this page (opened from the status
icon in the corner, before or after signing in) holds the proxy itself.

The page edits the same ``ProxyConfig`` the whole application connects with -
server, port, secret, enabled - and can test it or connect through it without
touching the saved session. The secret is written through the secure store
(DPAPI on Windows), never logged and never displayed in the clear.
"""
import logging
from dataclasses import replace
from typing import Callable, List, Optional

from PySide6 import QtWidgets

from teloude.infrastructure.telegram.connection import (
    ConnectionState,
    ProxyTestResult,
    TelegramConnection,
    redact,
)
from teloude.infrastructure.telegram.exceptions import ProxyConfigError
from teloude.infrastructure.telegram.proxy import ProxyConfig, ProxyKind
from teloude.ui import theme
from teloude.ui.components import GlassCard, fade_in, present_blocking
from teloude.ui.connection_indicator import ConnectionIndicator
from teloude.ui.workers import run_in_background

logger = logging.getLogger("ProxyDialog")

DEFAULT_PORT = 443


def connect_through(
    connection: TelegramConnection, config: ProxyConfig, phone: Optional[str]
) -> ProxyTestResult:
    """Tests a configuration, then moves the live session onto it.

    The test runs first on a throwaway session: a proxy that cannot reach
    Telegram never disturbs a working session. Only when it answers is the real
    connection switched - and the switch reaches every feature, because they all
    ask the connection layer for their client.
    """
    result = connection.test_proxy(config)
    if not result.ok or not phone:
        return result
    try:
        connection.reconnect(phone)
    except Exception as exc:
        message = redact(str(exc) or type(exc).__name__, config.secret)
        logger.warning("Switching the connection failed: %s", message)
        return ProxyTestResult(
            ok=False, state=ConnectionState.ERROR, message=message, proxy=config.endpoint()
        )
    return replace(
        result,
        connected_session=True,
        message=f"{result.message} Teloude is using it now.",
    )


class ProxyDialog(QtWidgets.QDialog):
    """Proxy configuration page (MTProto): server, port, secret, enable."""

    def __init__(
        self,
        connection: Optional[TelegramConnection],
        parent=None,
        bridge=None,
        phone_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        super().__init__(parent)
        self._connection = connection
        self._bridge = bridge
        self._phone_provider = phone_provider
        self._busy = False

        self.setWindowTitle("Connection settings")
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QtWidgets.QVBoxLayout(self)

        self.card = GlassCard()
        layout.addWidget(self.card)
        card_layout = QtWidgets.QVBoxLayout(self.card)
        card_layout.setContentsMargins(*([theme.SPACING["md"]] * 4))
        card_layout.setSpacing(theme.SPACING["sm"])

        # -- header: the same indicator the window shows, plus the switch -----
        self.indicator = ConnectionIndicator(connection, bridge, self)
        self.indicator.setToolTip("Current Telegram connection state.")
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(theme.SPACING["xs"])
        header.addWidget(self.indicator)
        title = QtWidgets.QLabel("Telegram connection")
        theme.apply_role(title, "section")
        header.addWidget(title)
        header.addStretch(1)
        self.enable_check = QtWidgets.QCheckBox("Use proxy")
        self.enable_check.setToolTip(
            "When enabled, every Telegram connection uses this proxy: sign-in, "
            "uploads, downloads, sync and search."
        )
        header.addWidget(self.enable_check)
        card_layout.addLayout(header)

        # -- the fields --------------------------------------------------------
        form = QtWidgets.QFormLayout()
        form.setSpacing(theme.SPACING["xs"])
        self.type_combo = QtWidgets.QComboBox()
        for kind in ProxyKind:
            self.type_combo.addItem(kind.label, kind)
        form.addRow("Proxy type:", self.type_combo)

        self.host_edit = QtWidgets.QLineEdit()
        self.host_edit.setPlaceholderText("mtproxy.example.com")
        form.addRow("Server:", self.host_edit)

        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        form.addRow("Port:", self.port_spin)

        secret_row = QtWidgets.QHBoxLayout()
        secret_row.setSpacing(theme.SPACING["xs"])
        self.secret_edit = QtWidgets.QLineEdit()
        self.secret_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Secret from Telegram")
        secret_row.addWidget(self.secret_edit)
        self.reveal_check = QtWidgets.QCheckBox("Show")
        self.reveal_check.toggled.connect(self._on_reveal)
        secret_row.addWidget(self.reveal_check)
        form.addRow("Secret:", secret_row)
        card_layout.addLayout(form)

        # -- state and storage note -------------------------------------------
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        card_layout.addWidget(self.status_label)

        self.storage_label = QtWidgets.QLabel("")
        theme.apply_role(self.storage_label, "caption")
        card_layout.addWidget(self.storage_label)

        # -- actions -----------------------------------------------------------
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(theme.SPACING["xs"])
        buttons.addStretch(1)
        self.test_button = QtWidgets.QPushButton("Test connection")
        self.test_button.clicked.connect(self._on_test)
        buttons.addWidget(self.test_button)
        self.connect_button = QtWidgets.QPushButton("Connect")
        theme.make_primary(self.connect_button)
        self.connect_button.setToolTip(
            "Save the proxy and connect through it. The proxy is tested first, "
            "so a mistake cannot break a working session."
        )
        self.connect_button.clicked.connect(self._on_connect)
        buttons.addWidget(self.connect_button)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        card_layout.addLayout(buttons)

        self.load(self._current())
        theme.normalize_layout_spacing(self)
        fade_in(self.card, lift_px=theme.SPACING["xs"], layout=layout)

    # -- data ----------------------------------------------------------------
    def _current(self) -> ProxyConfig:
        if self._connection is None:
            return ProxyConfig()
        return self._connection.proxy_config()

    def load(self, config: ProxyConfig) -> None:
        """Fills the page from a configuration (the saved one, or a test's)."""
        config = config.clean()
        index = self.type_combo.findData(config.kind)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        self.host_edit.setText(config.host)
        self.port_spin.setValue(config.port if config.port else DEFAULT_PORT)
        self.secret_edit.setText(config.secret)
        self.enable_check.setChecked(bool(config.enabled))
        if self._connection is None:
            for widget in (self.type_combo, self.host_edit, self.port_spin,
                           self.secret_edit, self.reveal_check, self.enable_check,
                           self.test_button, self.connect_button):
                widget.setEnabled(False)
            self.status_label.setText(
                "This build has no Telegram connection layer, so no proxy can be used."
            )
            self.storage_label.setText("")
            return
        self.indicator.set_state(
            ConnectionState.from_status(self._connection.status), self._connection.detail
        )
        self.storage_label.setText(
            f"Secret stored with: {self._connection.proxy_mechanism()}"
        )
        self.status_label.setText(self._connection.detail)

    def current_config(self) -> ProxyConfig:
        """What the fields say right now (validated by the actions, not here)."""
        return ProxyConfig(
            kind=self.type_combo.currentData() or ProxyKind.MT_PROTO,
            host=self.host_edit.text(),
            port=self.port_spin.value(),
            secret=self.secret_edit.text(),
            enabled=self.enable_check.isChecked(),
        ).clean()

    # -- interactions --------------------------------------------------------
    def _on_reveal(self, shown: bool) -> None:
        self.secret_edit.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if shown
            else QtWidgets.QLineEdit.EchoMode.Password
        )

    def _phone(self) -> Optional[str]:
        if self._phone_provider is None:
            return None
        try:
            return self._phone_provider()
        except Exception as exc:  # a missing session must not break the page
            logger.debug("No remembered phone: %s", type(exc).__name__)
            return None

    def _validated(self) -> ProxyConfig:
        """The fields as a usable configuration (raises ProxyConfigError).

        The enable switch decides whether the configuration is *saved*, not
        whether it can be tested: asking to test a proxy with the switch off
        must test the proxy, not the direct connection.
        """
        config = replace(self.current_config(), enabled=True)
        config.validate()
        return config

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for widget in (self.test_button, self.connect_button, self.cancel_button,
                       self.enable_check):
            widget.setEnabled(not busy)
        if message:
            self.status_label.setText(message)

    def _on_test(self) -> None:
        if self._busy or self._connection is None:
            return
        try:
            config = self._validated()
        except ProxyConfigError as exc:
            self._show_error(str(exc))
            return
        self.indicator.set_state(ConnectionState.CONNECTING, "Testing the connection...")
        self._set_busy(True, "Testing the connection to Telegram...")
        run_in_background(
            lambda: self._connection.test_proxy(config),
            on_done=self._after_test,
            on_error=self._after_failure,
        )

    def _after_test(self, result: ProxyTestResult) -> None:
        self._set_busy(False)
        self._show_result(result)

    def _on_connect(self) -> None:
        if self._busy or self._connection is None:
            return
        try:
            self._validated()  # the fields must be usable before anything is saved
        except ProxyConfigError as exc:
            self._show_error(str(exc))
            return
        config = self.current_config()  # saving honours the enable switch
        try:
            saved = self._connection.apply_proxy(config)
        except ProxyConfigError as exc:
            self._show_error(str(exc))
            return
        self.indicator.set_state(ConnectionState.CONNECTING, "Connecting...")
        self._set_busy(True, f"Connecting through {saved.endpoint()}..." if saved.enabled
                       else "Connecting to Telegram directly...")
        phone = self._phone()
        run_in_background(
            lambda: connect_through(self._connection, saved, phone),
            on_done=self._after_connect,
            on_error=self._after_failure,
        )

    def _after_connect(self, result: ProxyTestResult) -> None:
        self._set_busy(False)
        self._show_result(result)
        if result.ok:
            self.accept()

    def _after_failure(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(message)

    def _show_result(self, result: ProxyTestResult) -> None:
        self.indicator.set_state(result.state, result.message)
        self.status_label.setText(result.message)

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)
        if self._connection is not None:
            self.indicator.set_state(ConnectionState.ERROR, message)

    # -- convenience for tests / callers -------------------------------------
    @property
    def buttons(self) -> List[QtWidgets.QPushButton]:
        return [self.test_button, self.connect_button, self.cancel_button]


def open_proxy_settings(ctx, parent=None) -> int:
    """Opens the connection settings for an application context (blocking)."""
    connection = getattr(ctx, "connection", None)
    bridge = getattr(ctx, "bridge", None)
    services = getattr(ctx, "services", None)

    def phone() -> Optional[str]:
        try:
            return services.auth.remembered_phone()
        except Exception:
            return None

    dialog = ProxyDialog(connection, parent, bridge=bridge, phone_provider=phone)
    return present_blocking(dialog, parent)
