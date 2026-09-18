# teloude/ui/views/dashboard.py
"""Overview dashboard: storage cards, recent activity, quick actions."""
from PySide6 import QtCore, QtWidgets

from teloude.ui import theme


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
        theme.apply_role(header, "headline")      # was an inline font-size rule
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

        # indented by the card's own padding, so the heading lines up with the
        # text inside the card below it
        storages_label = theme.apply_role(QtWidgets.QLabel("My Storages"), "section",
                                          indent_px=theme.SPACING["xs"])
        layout.addWidget(storages_label)
        self.storage_list = QtWidgets.QListWidget()
        layout.addWidget(self.storage_list, 1)

        activity_label = theme.apply_role(QtWidgets.QLabel("Recent Activity"), "section",
                                          indent_px=theme.SPACING["xs"])
        layout.addWidget(activity_label)
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
        active = self._ctx.registry.active()
        active_storage_ids = {t.storage_id for t in active if t.storage_id}
        if not storages:
            self.storage_list.addItem("No storages yet - create one in the Storages tab.")
        for storage in storages:
            last = storage.last_backup_at or "never"
            state = "Backing up..." if storage.id in active_storage_ids else "Up to date"
            self.storage_list.addItem(
                f"{storage.name} - {format_bytes(storage.total_size)}, "
                f"{storage.file_count} files - {state} (last backup: {last})"
            )
        self.activity_list.clear()
        recent = self._ctx.repos.transfers.list_recent(8)
        if not recent:
            self.activity_list.addItem("No activity yet.")
        for transfer in recent:
            label = transfer.local_path or f"file {transfer.file_id}"
            self.activity_list.addItem(
                f"{transfer.kind} - {transfer.status} "
                f"({format_bytes(transfer.done_bytes)}/{format_bytes(transfer.total_bytes)}) - {label}"
            )
