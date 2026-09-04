# teloude/ui/views/storages.py
"""Storage management: create, adopt (multi-PC), refresh, delete."""
from PySide6 import QtCore, QtWidgets

from teloude.application.services import ServiceError
from teloude.ui.dialogs import confirm_destructive, show_error, show_info
from teloude.ui.workers import run_in_background


class StoragesView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.create_button = QtWidgets.QPushButton("Create storage")
        self.create_button.clicked.connect(self._on_create)
        self.adopt_button = QtWidgets.QPushButton("Adopt existing...")
        self.adopt_button.clicked.connect(self._on_adopt)
        self.refresh_button = QtWidgets.QPushButton("Refresh index")
        self.refresh_button.clicked.connect(self._on_refresh)
        self.delete_button = QtWidgets.QPushButton("Delete...")
        self.delete_button.clicked.connect(self._on_delete)
        for button in (self.create_button, self.adopt_button, self.refresh_button, self.delete_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Telegram", "Files", "Last backup"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        ctx.bridge.storages_changed.connect(lambda _p: self.refresh())
        self.refresh()

    def _selected_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(0x0100) if item else None

    def refresh(self) -> None:
        storages = self._ctx.services.storages.list()
        self.table.setRowCount(len(storages))
        for row, storage in enumerate(storages):
            name_item = QtWidgets.QTableWidgetItem(storage.name)
            name_item.setData(0x0100, storage.id)
            name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name_item)
            linked = "linked" if storage.telegram_chat_id else "not linked"
            self.table.setItem(row, 1, _readonly(f"{linked} (forum)" if storage.is_forum else linked))
            self.table.setItem(row, 2, _readonly(str(storage.file_count)))
            self.table.setItem(row, 3, _readonly(storage.last_backup_at or "never"))

    def _on_create(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "Create storage", "Storage name:")
        if not ok or not name.strip():
            return
        self.setEnabled(False)
        run_in_background(
            lambda: self._ctx.services.storages.create_storage(name.strip()),
            on_done=lambda _r: (self.setEnabled(True), self.refresh()),
            on_error=lambda msg: (self.setEnabled(True), show_error(self, "Create failed", msg)),
        )

    def _on_adopt(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Adopt storage", "Existing Telegram storage name:")
        if not ok or not name.strip():
            return
        self.setEnabled(False)
        run_in_background(
            lambda: self._ctx.services.storages.adopt_storage(name.strip()),
            on_done=lambda r: (
                self.setEnabled(True), self.refresh(),
                show_info(self, "Adopted", f"Storage '{r.name}' linked to Telegram."),
            ),
            on_error=lambda msg: (self.setEnabled(True), show_error(self, "Adopt failed", msg)),
        )

    def _on_refresh(self) -> None:
        storage_id = self._selected_id()
        if storage_id is None:
            show_info(self, "Refresh", "Select a storage first.")
            return
        self.setEnabled(False)
        run_in_background(
            lambda: self._ctx.services.storages.refresh_storage(storage_id),
            on_done=lambda n: (
                self.setEnabled(True), self.refresh(),
                show_info(self, "Refreshed", f"Imported {n} document(s) from Telegram."),
            ),
            on_error=lambda msg: (self.setEnabled(True), show_error(self, "Refresh failed", msg)),
        )

    def _on_delete(self) -> None:
        storage_id = self._selected_id()
        if storage_id is None:
            show_info(self, "Delete", "Select a storage first.")
            return
        storage = self._ctx.repos.storages.get(storage_id)
        if storage is None:
            return
        if not confirm_destructive(
            self, "Delete cloud backup?",
            f"This will remove the Telegram backup '{storage.name}'.\n\n"
            "Your local files will NOT be deleted.",
            "Delete Backup",
        ):
            return
        try:
            self._ctx.services.storages.delete_storage_cloud(storage_id)
        except ServiceError as exc:
            show_error(self, "Delete failed", str(exc))
        self.refresh()


def _readonly(text: str) -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(text)
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item
