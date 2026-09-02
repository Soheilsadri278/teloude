# teloude/ui/views/overview_view.py

from PySide6 import QtCore, QtWidgets, QtGui
from typing import TYPE_CHECKING
import logging

logger = logging.getLogger("OverviewView")

class OverviewWindow(QtWidgets.QMainWindow):
    """
    The main dashboard window view (UI Layer). 
    Handles UI layout and displays aggregated status without implementing business logic.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Teloude Backup - Overview")
        self.setGeometry(100, 100, 800, 600)
        self._setup_ui()

    def _setup_ui(self):
        # Central Widget Setup
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        layout = QtWidgets.QVBoxLayout(central_widget)
        
        # Header/Welcome Section (Simple Label Placeholder)
        header_label = QtWidgets.QLabel("Teloude Backup System")
        header_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header_label)

        # Storage Dashboard (Placeholder for dynamic content)
        storage_title = QtWidgets.QLabel("\nStorage Status:")
        storage_title.setStyleSheet("font-size: 18px; margin-top: 15px;")
        self.storage_list_widget = QtWidgets.QListWidget()
        layout.addWidget(storage_title)
        layout.addWidget(self.storage_list_widget)

        # Action Buttons (Placeholder for connection to AppServices)
        button_layout = QtWidgets.QHBoxLayout()
        self.backup_button = QtWidgets.QPushButton("Start Backup")
        self.restore_button = QtWidgets.QPushButton("Restore Files")
        self.search_button = QtWidgets.QPushButton("Search");

        button_layout.addWidget(self.backup_button)
        button_layout.addWidget(self.restore_button)
        button_layout.addWidget(self.search_button)
        
        layout.addLayout(button_layout)

    def display_status(self, storages: list):
        """Updates the storage dashboard based on provided data (simulated)."""
        self.storage_list_widget.clear()
        for i, storage in enumerate(storages):
            item = QtWidgets.QListWidgetItem(f"[{i+1}] {storage['name']} - Status: OK")
            # In a real app, this would populate size, count, etc.
            self.storage_list_widget.addItem(item)

    def show_status_message(self, message: str):
        """Simple method to display system messages."""
        print(f"[UI MESSAGE] {message}")