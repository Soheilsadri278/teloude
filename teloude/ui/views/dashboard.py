# teloude/ui/views/dashboard.py
"""Overview dashboard: storage cards, recent activity, quick actions."""
from PySide6 import QtCore, QtWidgets


def format_bytes(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class DashboardView(QtWidgets.QWidget):
    backup_requested = QtCore.Signal()
    restore_requested = QtCore.Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel("Teloude")
        header.setStyleSheet("font-size: 26px; font-weight: bold;")
        layout.addWidget(header)

        actions = QtWidgets.QHBoxLayout()
        self.backup_button = QtWidgets.QPushButton("Backup")
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.restore_button = QtWidgets.QPushButton("Restore")
        self.restore_button.clicked.connect(self.restore_requested.emit)
        actions.addWidget(self.backup_button)
        actions.addWidget(self.restore_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        layout.addWidget(QtWidgets.QLabel("My Storages"))
        self.storage_list = QtWidgets.QListWidget()
        layout.addWidget(self.storage_list, 1)

        layout.addWidget(QtWidgets.QLabel("Recent Activity"))
        self.activity_list = QtWidgets.QListWidget()
        layout.addWidget(self.activity_list, 1)

        ctx.bridge.storages_changed.connect(lambda _p: self.refresh())
        ctx.bridge.backup_done.connect(lambda _p: self.refresh())
        ctx.bridge.restore_done.connect(lambda _p: self.refresh())

    def showEvent(self, _event) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.storage_list.clear()
        storages = self._ctx.services.storages.list()
        if not storages:
            self.storage_list.addItem("No storages yet - create one in the Storages tab.")
        for storage in storages:
            last = storage.last_backup_at or "never"
            self.storage_list.addItem(
                f"{storage.name} - {format_bytes(storage.total_size)}, "
                f"{storage.file_count} files (last backup: {last})"
            )
        self.activity_list.clear()
        transfers = self._ctx.repos.transfers.list_active()
        recent = self._ctx.db.execute_query(
            "SELECT kind, status, total_bytes, done_bytes FROM transfers"
            " ORDER BY updated_at DESC LIMIT 8",
            fetch=True,
        )
        _ = transfers
        if not recent:
            self.activity_list.addItem("No activity yet.")
        for kind, status, total, done in recent:
            self.activity_list.addItem(f"{kind} - {status} ({format_bytes(done or 0)}/{format_bytes(total or 0)})")
