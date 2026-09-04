# teloude/ui/views/restore_view.py
"""Restore workflow: storage tree (files and folders) -> destination -> run."""
from PySide6 import QtCore, QtWidgets

from teloude.application.services import ServiceError
from teloude.ui.dialogs import make_collision_callback, show_error, show_info
from teloude.ui.views.dashboard import format_bytes


class RestoreView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self._updating_checks = False
        layout = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        self.storage_combo = QtWidgets.QComboBox()
        self.storage_combo.currentIndexChanged.connect(lambda _i: self._load_files())
        form.addRow("Storage:", self.storage_combo)
        dest_row = QtWidgets.QHBoxLayout()
        self.dest_edit = QtWidgets.QLineEdit()
        dest_browse = QtWidgets.QPushButton("Browse...")
        dest_browse.clicked.connect(self._on_browse_dest)
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(dest_browse)
        form.addRow("Destination:", dest_row)
        layout.addLayout(form)

        select_row = QtWidgets.QHBoxLayout()
        hint = QtWidgets.QLabel("Tick files, whole folders, or everything:")
        self.select_all = QtWidgets.QPushButton("Select all")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        self.select_none = QtWidgets.QPushButton("Select none")
        self.select_none.clicked.connect(lambda: self._set_all(False))
        select_row.addWidget(hint)
        select_row.addStretch(1)
        select_row.addWidget(self.select_all)
        select_row.addWidget(self.select_none)
        layout.addLayout(select_row)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Restore selected")
        self.start_button.clicked.connect(self._on_start)
        self.storage_button = QtWidgets.QPushButton("Restore entire storage...")
        self.storage_button.clicked.connect(self._on_restore_storage)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.storage_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QtWidgets.QLabel("Idle.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QtWidgets.QProgressBar()
        layout.addWidget(self.progress)

        bridge = ctx.bridge
        bridge.restore_progress.connect(self._on_progress)
        bridge.restore_done.connect(self._on_done)
        bridge.storages_changed.connect(lambda _p: self.refresh_storages())

    def showEvent(self, _event) -> None:
        self.refresh_storages()

    def refresh_storages(self) -> None:
        self.storage_combo.clear()
        for storage in self._ctx.services.storages.list():
            self.storage_combo.addItem(storage.name, storage.id)
        self._load_files()

    def _load_files(self) -> None:
        self.tree.clear()
        storage_id = self.storage_combo.currentData()
        if storage_id is None:
            return
        folders: dict = {}
        for record in self._ctx.repos.files.list_by_storage(storage_id):
            if not record.is_backed_up:
                continue
            parent = record.relative_path.rsplit("/", 1)[0] if "/" in record.relative_path else "(root)"
            folder_item = folders.get(parent)
            if folder_item is None:
                folder_item = QtWidgets.QTreeWidgetItem([parent, ""])
                folder_item.setFlags(folder_item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                folder_item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
                folders[parent] = folder_item
                self.tree.addTopLevelItem(folder_item)
            child = QtWidgets.QTreeWidgetItem(
                [record.relative_path.rsplit("/", 1)[-1], format_bytes(record.size)]
            )
            child.setData(0, QtCore.Qt.ItemDataRole.UserRole, record.id)
            child.setFlags(child.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
            folder_item.addChild(child)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def _on_item_changed(self, item, _column) -> None:
        if self._updating_checks:
            return
        self._updating_checks = True
        try:
            state = item.checkState(0)
            if item.childCount():
                for row in range(item.childCount()):
                    item.child(row).setCheckState(0, state)
            else:
                parent = item.parent()
                if parent is not None:
                    states = {parent.child(r).checkState(0) for r in range(parent.childCount())}
                    if len(states) == 1:
                        parent.setCheckState(0, states.pop())
                    else:
                        parent.setCheckState(0, QtCore.Qt.CheckState.PartiallyChecked)
        finally:
            self._updating_checks = False

    def _set_all(self, checked: bool) -> None:
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        self._updating_checks = True
        try:
            root = self.tree.invisibleRootItem()
            for row in range(root.childCount()):
                folder = root.child(row)
                folder.setCheckState(0, state)
                for child_row in range(folder.childCount()):
                    folder.child(child_row).setCheckState(0, state)
        finally:
            self._updating_checks = False

    def _on_browse_dest(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select restore destination")
        if folder:
            self.dest_edit.setText(folder)

    def _checked_ids(self):
        ids = []
        root = self.tree.invisibleRootItem()
        for row in range(root.childCount()):
            folder = root.child(row)
            for child_row in range(folder.childCount()):
                child = folder.child(child_row)
                if child.checkState(0) == QtCore.Qt.CheckState.Checked:
                    ids.append(child.data(0, QtCore.Qt.ItemDataRole.UserRole))
        return ids

    def _require_dest(self):
        dest = self.dest_edit.text().strip()
        if not dest:
            show_info(self, "Restore", "Choose a restore destination.")
            return None
        return dest

    def _on_start(self) -> None:
        file_ids = self._checked_ids()
        if not file_ids:
            show_info(self, "Restore", "Select at least one file or folder.")
            return
        dest = self._require_dest()
        if dest is None:
            return
        try:
            self._ctx.services.restore.start_files(
                file_ids, dest,
                collision_callback=make_collision_callback(self._ctx.asker),
            )
        except ServiceError as exc:
            show_error(self, "Restore failed to start", str(exc))
            return
        self.start_button.setEnabled(False)
        self.status_label.setText("Restore started...")

    def _on_restore_storage(self) -> None:
        storage_id = self.storage_combo.currentData()
        if storage_id is None:
            show_info(self, "Restore", "Select a storage first.")
            return
        dest = self._require_dest()
        if dest is None:
            return
        try:
            self._ctx.services.restore.start_storage(
                storage_id, dest,
                collision_callback=make_collision_callback(self._ctx.asker),
            )
        except ServiceError as exc:
            show_error(self, "Restore failed to start", str(exc))
            return
        self.start_button.setEnabled(False)
        self.status_label.setText("Full-storage restore started...")

    def _on_cancel(self) -> None:
        try:
            self._ctx.services.restore.cancel()
        except ServiceError as exc:
            show_info(self, "Restore", str(exc))

    @QtCore.Slot(dict)
    def _on_progress(self, payload: dict) -> None:
        total = payload.get("total_bytes") or 0
        done = payload.get("done_bytes") or 0
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        current = payload.get("current") or ""
        self.status_label.setText(
            f"{payload.get('done_files', 0)}/{payload.get('total_files', 0)} files - "
            f"{format_bytes(done)}/{format_bytes(total)} - {current}"
        )

    @QtCore.Slot(dict)
    def _on_done(self, payload: dict) -> None:
        self.start_button.setEnabled(True)
        failed = payload.get("failed", [])
        text = (f"Restored {payload.get('restored', 0)}, "
                f"skipped {payload.get('skipped', 0)}, failed {len(failed)}.")
        if payload.get("cancelled"):
            text += " Cancelled."
        self.status_label.setText(text)
        if failed:
            show_error(self, "Restore finished with errors",
                       "\n".join(f"{path}: {err}" for path, err in failed[:10]))
