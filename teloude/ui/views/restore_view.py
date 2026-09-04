# teloude/ui/views/restore_view.py
"""Restore workflow: pick storage/files -> destination -> collisions -> run."""
from PySide6 import QtCore, QtWidgets

from teloude.application.services import ServiceError
from teloude.core.restore import CollisionAction, CollisionDecision
from teloude.ui.dialogs import Answer, Question, show_error, show_info
from teloude.ui.views.dashboard import format_bytes


class RestoreView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
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
        self.select_all = QtWidgets.QPushButton("Select all")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        self.select_none = QtWidgets.QPushButton("Select none")
        self.select_none.clicked.connect(lambda: self._set_all(False))
        select_row.addWidget(self.select_all)
        select_row.addWidget(self.select_none)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self.file_list = QtWidgets.QListWidget()
        layout.addWidget(self.file_list, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Restore selected")
        self.start_button.clicked.connect(self._on_start)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(self.start_button)
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
        self.file_list.clear()
        storage_id = self.storage_combo.currentData()
        if storage_id is None:
            return
        for record in self._ctx.repos.files.list_by_storage(storage_id):
            if not record.is_backed_up:
                continue
            item = QtWidgets.QListWidgetItem(
                f"{record.relative_path} ({format_bytes(record.size)})"
            )
            item.setData(QtCore.Qt.ItemDataRole.UserRole, record.id)
            item.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.file_list.addItem(item)

    def _set_all(self, checked: bool) -> None:
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item is not None:
                item.setCheckState(state)

    def _on_browse_dest(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select restore destination")
        if folder:
            self.dest_edit.setText(folder)

    def _checked_ids(self):
        ids = []
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item is not None and item.checkState() == QtCore.Qt.CheckState.Checked:
                ids.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return ids

    def _on_start(self) -> None:
        file_ids = self._checked_ids()
        dest = self.dest_edit.text().strip()
        if not file_ids:
            show_info(self, "Restore", "Select at least one file.")
            return
        if not dest:
            show_info(self, "Restore", "Choose a restore destination.")
            return
        asker = self._ctx.asker

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
            choice = answer.choice if isinstance(answer.choice, CollisionAction) else CollisionAction.SKIP
            return CollisionDecision(choice, answer.checked)

        try:
            self._ctx.services.restore.start_files(file_ids, dest, collision_callback=ask_collision)
        except ServiceError as exc:
            show_error(self, "Restore failed to start", str(exc))
            return
        self.start_button.setEnabled(False)
        self.status_label.setText("Restore started...")

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
