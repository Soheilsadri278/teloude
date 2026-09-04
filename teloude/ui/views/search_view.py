# teloude/ui/views/search_view.py
"""Global search across indexed storages with restore / reveal / preview."""
import os

from PySide6 import QtCore, QtGui, QtWidgets

from teloude.ui.views.dashboard import format_bytes
from teloude.ui.workers import run_in_background


class SearchView(QtWidgets.QWidget):
    preview_requested = QtCore.Signal(str)  # local path for the preview pane

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QVBoxLayout(self)

        search_row = QtWidgets.QHBoxLayout()
        self.query_edit = QtWidgets.QLineEdit()
        self.query_edit.setPlaceholderText("File name or path...")
        self.query_edit.returnPressed.connect(self._on_search)
        self.search_button = QtWidgets.QPushButton("Search")
        self.search_button.clicked.connect(self._on_search)
        self.backed_only = QtWidgets.QCheckBox("Backed up only")
        search_row.addWidget(self.query_edit, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.backed_only)
        layout.addLayout(search_row)

        self.results = QtWidgets.QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["File", "Storage", "Path", "Size", "Status"])
        self.results.horizontalHeader().setStretchLastSection(True)
        self.results.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.results, 1)

        actions = QtWidgets.QHBoxLayout()
        self.preview_button = QtWidgets.QPushButton("Preview")
        self.preview_button.clicked.connect(self._on_preview)
        self.restore_button = QtWidgets.QPushButton("Restore...")
        self.restore_button.clicked.connect(self._on_restore)
        self.reveal_button = QtWidgets.QPushButton("Reveal local file")
        self.reveal_button.clicked.connect(self._on_reveal)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.restore_button)
        actions.addWidget(self.reveal_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._rows = []

    def _on_search(self) -> None:
        query = self.query_edit.text()
        backed_only = self.backed_only.isChecked()
        self.search_button.setEnabled(False)
        run_in_background(
            lambda: self._ctx.services.search.search(query, backed_only=backed_only),
            on_done=self._show_results,
            on_error=lambda _msg: self.search_button.setEnabled(True),
        )

    def _show_results(self, rows) -> None:
        self.search_button.setEnabled(True)
        self._rows = list(rows)
        self.results.setRowCount(len(self._rows))
        for row, result in enumerate(self._rows):
            status = "backed up" if result.is_backed_up else "not backed up"
            for col, text in ((0, result.file_name), (1, result.storage_name),
                              (2, result.relative_path), (3, format_bytes(result.size)),
                              (4, status)):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.results.setItem(row, col, item)

    def _selected(self):
        row = self.results.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _on_preview(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        record = self._ctx.repos.files.get(selected.file_id)
        if record is not None and os.path.exists(record.local_path):
            self.preview_requested.emit(record.local_path)

    def _on_restore(self) -> None:
        from teloude.application.services import ServiceError
        from teloude.ui.dialogs import make_collision_callback, show_error, show_info

        selected = self._selected()
        if selected is None:
            return
        dest = QtWidgets.QFileDialog.getExistingDirectory(self, "Select restore destination")
        if not dest:
            return
        try:
            self._ctx.services.restore.start_files(
                [selected.file_id], dest,
                collision_callback=make_collision_callback(self._ctx.asker),
            )
        except ServiceError as exc:
            show_error(self, "Restore failed to start", str(exc))
            return
        show_info(self, "Restore", "Restore started - watch progress in Transfers.")

    def _on_reveal(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        record = self._ctx.repos.files.get(selected.file_id)
        if record is None:
            return
        parent = str(QtCore.QFileInfo(record.local_path).absolutePath())
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(parent))
