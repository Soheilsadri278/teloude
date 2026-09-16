# teloude/ui/views/transfers_view.py
"""Transfer center: live queue with pause / resume / retry / cancel."""
from PySide6 import QtCore, QtWidgets

from teloude.ui.views.dashboard import format_bytes


class TransfersView(QtWidgets.QWidget):
    """Live queue + recent history.

    Progress events arrive several times per second, so refreshes are coalesced
    and identical content is not rebuilt: re-creating every row on each tick
    would reset the user's selection and flicker the table.
    """

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self._last_signature = None
        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.pause_button.clicked.connect(lambda: self._act("pause"))
        self.resume_button = QtWidgets.QPushButton("Resume")
        self.resume_button.clicked.connect(lambda: self._act("resume"))
        self.retry_button = QtWidgets.QPushButton("Retry")
        self.retry_button.clicked.connect(lambda: self._act("retry"))
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self._act("cancel"))
        for button in (self.pause_button, self.resume_button, self.retry_button, self.cancel_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Kind", "File", "Status", "Progress", "Error"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        # coalesce bursts of progress events into at most ~5 rebuilds per second
        self._coalesce = QtCore.QTimer(self)
        self._coalesce.setSingleShot(True)
        self._coalesce.setInterval(200)
        self._coalesce.timeout.connect(self.refresh)
        ctx.bridge.backup_progress.connect(self._on_progress)
        ctx.bridge.restore_progress.connect(self._on_progress)

    def showEvent(self, _event) -> None:
        self.refresh()
        self._timer.start()

    def hideEvent(self, _event) -> None:
        self._timer.stop()

    def _on_progress(self, _payload) -> None:
        if self.isVisible() and not self._coalesce.isActive():
            self._coalesce.start()

    def _rows(self):
        active = self._ctx.registry.active()
        history = self._ctx.repos.transfers.list_history(50)
        rows = [("live", t) for t in active]
        rows += [("hist", h) for h in history]
        return rows

    @staticmethod
    def _signature(rows) -> tuple:
        return tuple(
            (origin, entry.id, entry.status, entry.done_bytes, entry.error or "")
            for origin, entry in rows
        )

    def refresh(self) -> None:
        rows = self._rows()
        signature = self._signature(rows)
        if signature == self._last_signature:
            return  # nothing changed: rebuilding would only reset the selection
        selected = self._selected_transfer()
        restore_row = None
        self._last_signature = signature
        self.table.setRowCount(len(rows))
        for row, (origin, entry) in enumerate(rows):
            kind, label = entry.kind, entry.local_path or f"file {entry.file_id}"
            status, total, done, error = (
                entry.status, entry.total_bytes, entry.done_bytes, entry.error or "",
            )
            # history rows carry their id too: retrying a failed transfer is
            # exactly what the Retry button is for (TransferRegistry.retry
            # accepts failed/cancelled rows).
            tid = entry.id
            self.table.setItem(row, 0, _cell(kind))
            self.table.setItem(row, 1, _cell(str(label)))
            self.table.setItem(row, 2, _cell(status))
            pct = f"{format_bytes(done)}/{format_bytes(total)}"
            self.table.setItem(row, 3, _cell(pct))
            self.table.setItem(row, 4, _cell(error))
            item = self.table.item(row, 0)
            if item is not None and tid is not None:
                item.setData(QtCore.Qt.ItemDataRole.UserRole, tid)
                if tid == selected:
                    restore_row = row
        if restore_row is not None:
            self.table.selectRow(restore_row)

    def _selected_transfer(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None

    def _act(self, action: str) -> None:
        transfer_id = self._selected_transfer()
        if transfer_id is None:
            return
        registry = self._ctx.registry
        try:
            if action == "pause":
                registry.pause(transfer_id)
            elif action == "resume":
                registry.resume(transfer_id)
            elif action == "retry":
                registry.retry(transfer_id)
            elif action == "cancel":
                registry.cancel(transfer_id)
        except Exception:
            pass  # illegal transition for this row's state; selection may be stale
        self.refresh()


def _cell(text: str) -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(text)
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item
