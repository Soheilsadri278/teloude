# teloude/ui/views/preview_widget.py
"""Preview pane: thumbnail + metadata for a local file (worker thread)."""
from PySide6 import QtCore, QtGui, QtWidgets

from teloude.core.preview import generate_preview
from teloude.ui.workers import run_in_background


class PreviewWidget(QtWidgets.QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        layout = QtWidgets.QVBoxLayout(self)
        self.image_label = QtWidgets.QLabel("Select a file to preview.")
        self.image_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(240)
        layout.addWidget(self.image_label, 1)
        self.detail_label = QtWidgets.QLabel("")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

    @QtCore.Slot(str)
    def show_path(self, local_path: str) -> None:
        self.detail_label.setText("Loading preview...")
        cache_dir = self._ctx.preview_cache_dir
        run_in_background(
            lambda: generate_preview(local_path, cache_dir=cache_dir),
            on_done=self._show_result,
            on_error=lambda msg: self.detail_label.setText(f"Preview unavailable: {msg}"),
        )

    def _show_result(self, result) -> None:
        self.detail_label.setText(result.detail or result.kind)
        if result.thumbnail_png:
            pixmap = QtGui.QPixmap()
            pixmap.loadFromData(result.thumbnail_png, "PNG")
            self.image_label.setPixmap(pixmap.scaled(
                360, 360, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation))
        else:
            self.image_label.setText("No thumbnail available.")
