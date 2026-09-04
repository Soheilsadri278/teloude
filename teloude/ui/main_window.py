# teloude/ui/main_window.py
"""Main application window: navigation, views, status bar, tray behavior."""
from PySide6 import QtCore, QtWidgets

from teloude.ui.views.backup_view import BackupView
from teloude.ui.views.dashboard import DashboardView
from teloude.ui.views.preview_widget import PreviewWidget
from teloude.ui.views.restore_view import RestoreView
from teloude.ui.views.search_view import SearchView
from teloude.ui.views.settings_view import SettingsView
from teloude.ui.views.storages import StoragesView
from teloude.ui.views.transfers_view import TransfersView


class MainWindow(QtWidgets.QMainWindow):
    exit_requested = QtCore.Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self.setWindowTitle("Teloude Backup")
        self.setMinimumSize(900, 640)

        self.nav = QtWidgets.QListWidget()
        self.nav.setMaximumWidth(160)
        self.stack = QtWidgets.QStackedWidget()

        self.dashboard = DashboardView(ctx)
        self.storages = StoragesView(ctx)
        self.backup = BackupView(ctx)
        self.transfers = TransfersView(ctx)
        self.restore = RestoreView(ctx)
        self.search = SearchView(ctx)
        self.preview = PreviewWidget(ctx)
        self.settings = SettingsView(ctx)

        search_with_preview = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        search_with_preview.addWidget(self.search)
        search_with_preview.addWidget(self.preview)
        search_with_preview.setSizes([420, 200])
        self.search.preview_requested.connect(self.preview.show_path)

        pages = [
            ("Overview", self.dashboard),
            ("Storages", self.storages),
            ("Backup", self.backup),
            ("Transfers", self.transfers),
            ("Restore", self.restore),
            ("Search", search_with_preview),
            ("Settings", self.settings),
        ]
        for title, widget in pages:
            self.nav.addItem(title)
            self.stack.addWidget(widget)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self.dashboard.backup_requested.connect(lambda: self._goto("Backup"))
        self.dashboard.restore_requested.connect(lambda: self._goto("Restore"))

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.nav)
        splitter.addWidget(self.stack)
        splitter.setSizes([160, 740])
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Ready.")
        ctx.bridge.auth_state.connect(self._on_auth_state)

    def _goto(self, title: str) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item is not None and item.text() == title:
                self.nav.setCurrentRow(row)
                return

    @QtCore.Slot(dict)
    def _on_auth_state(self, payload: dict) -> None:
        self.statusBar().showMessage(f"Telegram: {payload.get('state', '?')}")

    def closeEvent(self, event) -> None:
        # Minimize to tray instead of quitting; transfers keep running.
        tray = self._ctx.tray
        if tray is None or not tray.is_supported:
            event.accept()
            return
        event.ignore()
        self.hide()
        tray.show_message("Teloude", "Backup continues in the system tray.")
