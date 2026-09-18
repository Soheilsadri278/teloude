# teloude/ui/views/restore_view.py
"""Restore workflow: storage tree (files and folders) -> destination -> run.

Pause / Resume / Cancel act on the running restore and mirror its real state
(Bug 3): the buttons only offer commands that can change something, and the
status line names what the transfer is actually doing.

The tree is built lazily: a storage with tens of thousands of files only creates
one row per folder up front, and a folder's rows appear when it is expanded.
Selection is tracked per folder (not per widget), so ticking a folder selects
files that were never rendered - and a 50k-file storage opens instantly instead
of freezing the UI while it builds 50k widgets.
"""
from dataclasses import dataclass, field
from typing import Dict, List

from PySide6 import QtCore, QtWidgets

from teloude.application.services import ServiceError
from teloude.ui.dialogs import make_collision_callback, show_error, show_info
from teloude.ui.views.dashboard import format_bytes
from teloude.ui import theme
from teloude.ui.components import make_action_bar
from teloude.ui.views.run_state import STARTING, can_start, controls_for, is_active, state_label

CHECKED = QtCore.Qt.CheckState.Checked
UNCHECKED = QtCore.Qt.CheckState.Unchecked
PARTIAL = QtCore.Qt.CheckState.PartiallyChecked


@dataclass
class _Folder:
    """One rendered folder row plus the files it stands for (rendered or not)."""

    item: QtWidgets.QTreeWidgetItem
    records: List[object] = field(default_factory=list)
    default_checked: bool = False
    overrides: Dict[int, bool] = field(default_factory=dict)
    populated: bool = False

    @property
    def total_size(self) -> int:
        return sum(getattr(record, "size", 0) for record in self.records)

    def is_checked(self, file_id: int) -> bool:
        return self.overrides.get(file_id, self.default_checked)

    def selected_ids(self) -> List[int]:
        return [r.id for r in self.records if self.is_checked(r.id)]

    def selected_count(self) -> int:
        return sum(1 for r in self.records if self.is_checked(r.id))

    def check_state(self) -> QtCore.Qt.CheckState:
        selected = self.selected_count()
        if selected == 0:
            return UNCHECKED
        if selected == len(self.records):
            return CHECKED
        return PARTIAL


class RestoreView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self._updating_checks = False
        self._folders: List[_Folder] = []
        self._updating_tree = False
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
        self.tree.setUniformRowHeights(True)  # big storages stay responsive
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self.tree, 1)

        self.summary_label = QtWidgets.QLabel("")
        layout.addWidget(self.summary_label)

        self.start_button = QtWidgets.QPushButton("Restore selected")
        theme.make_primary(self.start_button)      # the page's primary action
        self.start_button.clicked.connect(self._on_start)
        self.storage_button = QtWidgets.QPushButton("Restore entire storage...")
        self.storage_button.clicked.connect(self._on_restore_storage)
        self.pause_button = QtWidgets.QPushButton("\u23f8 Pause")
        self.pause_button.clicked.connect(self._on_pause)
        self.resume_button = QtWidgets.QPushButton("\u25b6 Resume")
        self.resume_button.clicked.connect(self._on_resume)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        self.action_bar = make_action_bar(
            self.start_button, self.storage_button, self.pause_button,
            self.resume_button, self.cancel_button,
        )
        layout.addWidget(self.action_bar)

        self.status_label = QtWidgets.QLabel("Idle.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QtWidgets.QProgressBar()
        layout.addWidget(self.progress)
        # Content stays at the top: without this the wrapped status label takes
        # the page's spare height and everything drifts towards the middle.
        layout.addStretch(1)

        self._state = None      # last state the engine actually reported
        self._detail = ""       # progress / result text under the buttons

        bridge = ctx.bridge
        bridge.restore_progress.connect(self._on_progress)
        bridge.restore_done.connect(self._on_done)
        bridge.transfer_state.connect(self._on_transfer_state)
        bridge.storages_changed.connect(lambda _p: self.refresh_storages())
        self._apply_state()

    def showEvent(self, _event) -> None:
        self.refresh_storages()
        # Never assume: ask the service what the run is really doing right now.
        self._apply_state(self._ctx.services.restore.state)

    # -- real state, never guessed (Bug 3) --------------------------------
    def _on_transfer_state(self, payload: dict) -> None:
        if payload.get("kind") != "restore":
            return
        self._apply_state(payload.get("state"))

    def _apply_state(self, state=None) -> None:
        """Enables only the commands that can change the real state."""
        if state is not None:
            self._state = state.value if hasattr(state, "value") else str(state)
        can_pause, can_resume, can_cancel = controls_for(self._state)
        self.pause_button.setEnabled(can_pause)
        self.resume_button.setEnabled(can_resume)
        self.cancel_button.setEnabled(can_cancel)
        startable = can_start(self._state)
        self.start_button.setEnabled(startable)
        self.storage_button.setEnabled(startable)
        # Same rule as Backup: depth follows the real, reported state.
        self.action_bar.set_lifted(self._state == STARTING or is_active(self._state))
        self._render_status()

    def _render_status(self) -> None:
        label = state_label("restore", self._state)
        parts = [part for part in (label, self._detail) if part]
        self.status_label.setText(": ".join(parts) if parts else "Idle.")

    def refresh_storages(self) -> None:
        self.storage_combo.clear()
        for storage in self._ctx.services.storages.list():
            self.storage_combo.addItem(storage.name, storage.id)
        self._load_files()

    def _load_files(self) -> None:
        """Builds one row per folder; file rows are created when a folder opens."""
        self._updating_tree = True
        try:
            self.tree.clear()
            self._folders = []
            storage_id = self.storage_combo.currentData()
            if storage_id is None:
                self.summary_label.setText("")
                return
            grouped: Dict[str, List[object]] = {}
            for record in self._ctx.repos.files.list_by_storage(storage_id):
                if not record.is_backed_up:
                    continue
                parent = (
                    record.relative_path.rsplit("/", 1)[0]
                    if "/" in record.relative_path
                    else "(root)"
                )
                grouped.setdefault(parent, []).append(record)
            for parent in sorted(grouped):
                records = grouped[parent]
                item = QtWidgets.QTreeWidgetItem([parent, format_bytes(
                    sum(r.size for r in records)
                )])
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, UNCHECKED)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, len(records))
                self.tree.addTopLevelItem(item)
                self._folders.append(_Folder(item=item, records=records))
            total_files = sum(len(f.records) for f in self._folders)
            self.summary_label.setText(
                f"{len(self._folders)} folder(s), {total_files} backed-up file(s). "
                "Tick a folder to select everything in it, or expand it to pick "
                "individual files."
            )
        finally:
            self._updating_tree = False

    def _populate(self, folder: _Folder) -> None:
        """Creates the child rows of a folder on first expansion."""
        if folder.populated:
            return
        self._updating_tree = True
        try:
            folder.item.takeChildren()  # drop the 'expand me' placeholder, if any
            for record in folder.records:
                child = QtWidgets.QTreeWidgetItem(
                    [record.relative_path.rsplit("/", 1)[-1], format_bytes(record.size)]
                )
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole, record.id)
                child.setFlags(child.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(
                    0, CHECKED if folder.is_checked(record.id) else UNCHECKED
                )
                folder.item.addChild(child)
            folder.populated = True
        finally:
            self._updating_tree = False

    def _folder_of(self, item: QtWidgets.QTreeWidgetItem):
        parent = item.parent() or item
        for folder in self._folders:
            if folder.item is parent:
                return folder
        return None

    @QtCore.Slot(QtWidgets.QTreeWidgetItem)
    def _on_item_expanded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._updating_tree:
            return
        folder = self._folder_of(item)
        if folder is not None:
            self._populate(folder)

    def _on_item_changed(self, item, _column) -> None:
        if self._updating_checks or self._updating_tree:
            return
        folder = self._folder_of(item)
        if folder is None:
            return
        state = item.checkState(0)
        self._updating_checks = True
        try:
            if item is folder.item:
                # folder row: applies to every file in it, rendered or not
                folder.default_checked = state == CHECKED
                folder.overrides = {}
                for row in range(item.childCount()):
                    item.child(row).setCheckState(
                        0, CHECKED if folder.default_checked else UNCHECKED
                    )
            else:
                file_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
                folder.overrides[file_id] = state == CHECKED
                folder.item.setCheckState(0, folder.check_state())
        finally:
            self._updating_checks = False

    def _set_all(self, checked: bool) -> None:
        self._updating_checks = True
        self._updating_tree = True
        try:
            for folder in self._folders:
                folder.default_checked = checked
                folder.overrides = {}
                folder.item.setCheckState(0, CHECKED if checked else UNCHECKED)
                if folder.populated:
                    for row in range(folder.item.childCount()):
                        folder.item.child(row).setCheckState(
                            0, CHECKED if checked else UNCHECKED
                        )
        finally:
            self._updating_tree = False
            self._updating_checks = False

    def _on_browse_dest(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select restore destination")
        if folder:
            self.dest_edit.setText(folder)

    def _checked_ids(self):
        """Every selected file id - including files inside unexpanded folders."""
        ids: List[int] = []
        for folder in self._folders:
            ids.extend(folder.selected_ids())
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
        # The run reports its own state as soon as it starts; until that
        # arrives the view says "starting" and offers no control at all.
        self._detail = ""
        self._apply_state(STARTING)

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
        self._detail = ""
        self._apply_state(STARTING)

    def _on_pause(self) -> None:
        self._command(self._ctx.services.restore.pause)

    def _on_resume(self) -> None:
        self._command(self._ctx.services.restore.resume)

    def _on_cancel(self) -> None:
        self._command(self._ctx.services.restore.cancel)

    def _command(self, fn) -> None:
        try:
            fn()
        except ServiceError as exc:
            show_info(self, "Restore", str(exc))

    @QtCore.Slot(dict)
    def _on_progress(self, payload: dict) -> None:
        total = payload.get("total_bytes") or 0
        done = payload.get("done_bytes") or 0
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        current = payload.get("current") or ""
        self._detail = (
            f"{payload.get('done_files', 0)}/{payload.get('total_files', 0)} files - "
            f"{format_bytes(done)}/{format_bytes(total)} - {current}"
        )
        self._render_status()

    @QtCore.Slot(dict)
    def _on_done(self, payload: dict) -> None:
        failed = payload.get("failed", [])
        text = (f"Restored {payload.get('restored', 0)}, "
                f"skipped {payload.get('skipped', 0)}, failed {len(failed)}.")
        if payload.get("cancelled"):
            text += " Cancelled."
        self._detail = text
        # The outcome event arrives separately; keep the buttons consistent in
        # case this page was built before the run it reports on.
        self._apply_state()
        if failed:
            show_error(self, "Restore finished with errors",
                       "\n".join(f"{path}: {err}" for path, err in failed[:10]))
