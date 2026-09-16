# teloude/ui/views/backup_view.py
"""Backup workflow: storage + folder + policy -> plan -> run with progress."""
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from teloude.application.services import ServiceError
from teloude.core.duplicates import DuplicateAction, DuplicateDecision
from teloude.infrastructure.telegram.exceptions import TeloudeTelegramError
from teloude.ui.dialogs import Answer, Question, show_error, show_info
from teloude.ui.views.dashboard import format_bytes
from teloude.ui.views.run_state import STARTING, can_start, controls_for, state_label


class BackupView(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        self.storage_combo = QtWidgets.QComboBox()
        form.addRow("Storage:", self.storage_combo)
        folder_row = QtWidgets.QHBoxLayout()
        self.folder_edit = QtWidgets.QLineEdit()
        self.folder_edit.setPlaceholderText("Select a local folder to back up")
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._on_browse)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Folder:", folder_row)
        self.policy_combo = QtWidgets.QComboBox()
        self.policy_combo.addItem("Ask me for each duplicate", "ask")
        self.policy_combo.addItem("Skip duplicates", "skip_all")
        self.policy_combo.addItem("Upload duplicates again", "upload_all")
        form.addRow("Duplicates:", self.policy_combo)
        self.verify_check = QtWidgets.QCheckBox(
            "Verify file contents again (slower, ignores unchanged-by-size-and-time)"
        )
        self.verify_check.setToolTip(
            "By default a file whose size and modification time are unchanged is "
            "skipped without re-reading it. Tick this to SHA-256 every file."
        )
        form.addRow("", self.verify_check)
        layout.addLayout(form)

        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start backup")
        self.start_button.clicked.connect(self._on_start)
        self.pause_button = QtWidgets.QPushButton("\u23f8 Pause")
        self.pause_button.clicked.connect(self._on_pause)
        self.resume_button = QtWidgets.QPushButton("\u25b6 Resume")
        self.resume_button.clicked.connect(self._on_resume)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        for button in (self.start_button, self.pause_button, self.resume_button, self.cancel_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QtWidgets.QLabel("Idle.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QtWidgets.QProgressBar()
        layout.addWidget(self.progress)

        self._state = None      # last state the engine actually reported
        self._detail = ""       # progress / result text under the buttons

        bridge = ctx.bridge
        bridge.backup_planned.connect(self._on_planned)
        bridge.backup_progress.connect(self._on_progress)
        bridge.backup_done.connect(self._on_done)
        bridge.transfer_state.connect(self._on_transfer_state)
        bridge.storages_changed.connect(lambda _p: self.refresh_storages())
        self.refresh_storages()
        self._apply_state()

    def showEvent(self, _event) -> None:
        self.refresh_storages()
        # Never assume: ask the service what the run is really doing right now.
        self._apply_state(self._ctx.services.backup.state)

    # -- real state, never guessed (Bug 3) --------------------------------
    def _on_transfer_state(self, payload: dict) -> None:
        if payload.get("kind") != "backup":
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
        self.start_button.setEnabled(can_start(self._state))
        self._render_status()

    def _render_status(self) -> None:
        label = state_label("backup", self._state)
        parts = [part for part in (label, self._detail) if part]
        self.status_label.setText(": ".join(parts) if parts else "Idle.")

    def refresh_storages(self) -> None:
        current = self.storage_combo.currentData()
        self.storage_combo.clear()
        for storage in self._ctx.services.storages.list():
            self.storage_combo.addItem(storage.name, storage.id)
        if current is not None:
            index = self.storage_combo.findData(current)
            if index >= 0:
                self.storage_combo.setCurrentIndex(index)

    def _on_browse(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder to back up")
        if folder:
            self.folder_edit.setText(folder)

    def _on_start(self) -> None:
        storage_id = self.storage_combo.currentData()
        folder = self.folder_edit.text().strip()
        if storage_id is None:
            show_info(self, "Backup", "Create a storage first (Storages tab).")
            return
        if not folder:
            show_info(self, "Backup", "Select a local folder first.")
            return
        source = Path(folder)
        if not source.exists():
            show_info(self, "Backup", f"The folder '{folder}' does not exist.")
            return
        if not source.is_dir():
            show_info(self, "Backup", f"'{folder}' is a file, not a folder.")
            return
        policy = self.policy_combo.currentData()
        asker = self._ctx.asker

        def ask_duplicate(match, index, total):
            answer: Answer = asker.ask(Question(
                title="Duplicate found",
                text=f"'{match.scanned.relative}' is identical to '{match.existing_path}' "
                     f"in storage '{match.existing_storage}' ({index}/{total}).",
                options=[("Skip", DuplicateAction.SKIP),
                         ("Upload again", DuplicateAction.UPLOAD_AGAIN),
                         ("Cancel backup", DuplicateAction.CANCEL)],
                check_text="Apply to all",
            ))
            choice = answer.choice if isinstance(answer.choice, DuplicateAction) else DuplicateAction.SKIP
            return DuplicateDecision(choice, answer.checked)

        try:
            self._ctx.services.backup.start(
                storage_id, folder, policy=policy, ask_callback=ask_duplicate,
                verify_content=self.verify_check.isChecked(),
            )
        except (ServiceError, TeloudeTelegramError) as exc:
            show_error(self, "Backup failed to start", str(exc))
            return
        # The run reports its own state as soon as it starts; until that
        # arrives the view says "starting" and offers no control at all.
        self._detail = ""
        self._apply_state(STARTING)

    def _on_pause(self) -> None:
        self._guard(self._ctx.services.backup.pause)

    def _on_resume(self) -> None:
        self._guard(self._ctx.services.backup.resume)

    def _on_cancel(self) -> None:
        self._guard(lambda: self._ctx.services.backup.cancel())

    def _guard(self, fn) -> None:
        try:
            fn()
        except ServiceError as exc:
            show_info(self, "Backup", str(exc))

    @QtCore.Slot(dict)
    def _on_planned(self, payload: dict) -> None:
        self._detail = f"Backing up {payload.get('files', 0)} file(s)..."
        self._render_status()

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
        if payload.get("error"):
            show_error(self, "Backup failed", "The backup run failed unexpectedly. See logs.")
            self._detail = "the backup run failed unexpectedly. See logs."
            self._apply_state()
            return
        failed = payload.get("failed", [])
        text = (f"Uploaded {payload.get('uploaded', 0)}, "
                f"skipped {payload.get('skipped', 0)}, failed {len(failed)}.")
        unchanged = payload.get("unchanged", 0)
        if unchanged:
            text += f" {unchanged} already up to date."
        if payload.get("cancelled"):
            text += " Cancelled."
        self._detail = text
        # The outcome event arrives separately; keep the buttons consistent in
        # case this page was built before the run it reports on.
        self._apply_state()
        if failed:
            show_error(self, "Backup finished with errors",
                       "\n".join(f"{path}: {err}" for path, err in failed[:10]))
