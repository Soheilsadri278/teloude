# teloude/ui/views/settings_view.py
"""Settings: appearance, transfer speed limit, session lock, directories, about."""
from PySide6 import QtWidgets

from teloude.application.services import ServiceError
from teloude.core.speed_limiter import SpeedLimiter, limit_from_mbps
from teloude.ui import theme
from teloude.ui.dialogs import show_error, show_info


class SettingsView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QFormLayout(self)

        # Appearance (light is the default): applied immediately and remembered
        # through the existing settings service - no new storage mechanism.
        self.appearance_combo = QtWidgets.QComboBox()
        self.appearance_combo.addItem("Light", "light")
        self.appearance_combo.addItem("Dark", "dark")
        current = theme.stored_appearance(ctx.services)
        for row in range(self.appearance_combo.count()):
            if self.appearance_combo.itemData(row) == current:
                self.appearance_combo.setCurrentIndex(row)
                break
        self.appearance_combo.currentIndexChanged.connect(self._on_appearance)
        layout.addRow("Appearance:", self.appearance_combo)

        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItem("Unlimited", None)
        self.speed_combo.addItem("10 MB/s", 10.0)
        self.speed_combo.addItem("5 MB/s", 5.0)
        self.speed_combo.addItem("2 MB/s", 2.0)
        self.speed_combo.addItem("Custom...", "custom")
        self.custom_spin = QtWidgets.QDoubleSpinBox()
        self.custom_spin.setRange(0.1, 1000.0)
        self.custom_spin.setSuffix(" MB/s")
        self.custom_spin.setEnabled(False)
        self.speed_combo.currentIndexChanged.connect(
            lambda _i: self.custom_spin.setEnabled(self.speed_combo.currentData() == "custom")
        )
        speed_row = QtWidgets.QHBoxLayout()
        speed_row.addWidget(self.speed_combo)
        speed_row.addWidget(self.custom_spin)
        layout.addRow("Speed limit:", speed_row)

        apply_button = QtWidgets.QPushButton("Apply speed limit")
        apply_button.clicked.connect(self._on_apply_speed)
        layout.addRow(apply_button)

        session_row = QtWidgets.QHBoxLayout()
        self.session_label = QtWidgets.QLabel(ctx.session_store.mechanism)
        lock_button = QtWidgets.QPushButton("Lock session now")
        lock_button.clicked.connect(self._on_lock_session)
        # The label no longer pushes the button to the far edge: the row reads
        # left to right, the button right where its label is.
        session_row.addWidget(self.session_label)
        session_row.addWidget(lock_button)
        session_row.addStretch(1)
        layout.addRow("Session protection:", session_row)

        layout.addRow("Data directory:", QtWidgets.QLabel(str(ctx.config.get_data_dir())))
        layout.addRow("Session directory:", QtWidgets.QLabel(str(ctx.config.get_session_dir())))
        layout.addRow("Version:", QtWidgets.QLabel(ctx.config.app_version))
        self._load_current_speed()

    def _on_appearance(self, _index: int) -> None:
        choice = self.appearance_combo.currentData() or "light"
        theme.remember_appearance(self._ctx.services, choice)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            theme.apply_theme(app, dark=(choice == "dark"))

    def _load_current_speed(self) -> None:
        current = self._ctx.services.settings.get_speed_limit_mbps()
        for row in range(self.speed_combo.count()):
            if self.speed_combo.itemData(row) == current:
                self.speed_combo.setCurrentIndex(row)
                return
        if current:
            self.speed_combo.setCurrentIndex(self.speed_combo.findData("custom"))
            self.custom_spin.setValue(current)
            self.custom_spin.setEnabled(True)

    def _on_apply_speed(self) -> None:
        choice = self.speed_combo.currentData()
        mbps = self.custom_spin.value() if choice == "custom" else choice
        try:
            self._ctx.services.settings.set_speed_limit_mbps(mbps)
            self._ctx.apply_speed_limit(SpeedLimiter(limit_from_mbps(mbps)))
        except (ServiceError, ValueError) as exc:
            show_error(self, "Invalid speed limit", str(exc))
            return
        show_info(self, "Settings", "Speed limit updated.")

    def _on_lock_session(self) -> None:
        if self._ctx.session_store.lock():
            show_info(self, "Session", "Session locked. It will unlock on next start.")
        else:
            show_info(self, "Session", "No active session file to lock.")
