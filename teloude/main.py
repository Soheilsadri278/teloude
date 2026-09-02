# teloude/main.py

import sys
from PySide6 import QtWidgets
from teloude.config import AppConfig
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.core.scanner.file_scanner import scan_directory, FileMetadata
import logging

def run_main_application():
    """Entry point for the PySide6 GUI."""
    # 1. Load Configuration and Initialize Core Services (Done in __init__.py/AppConfig)
    config = AppConfig()

    # 2. Setup Logging
    from teloude.infrastructure.logging import setup_logging
    setup_logging(config)
    logger = logging.getLogger("MainApp")

    try:
        # 3. Initialize DB and Run Migrations (Done in __init__.py/DatabaseManager)
        db_manager = DatabaseManager(config)
        if not db_manager.initialize():
            logger.error("Application cannot start due to database failure.")
            return

        # 4. Open PySide6 Main Window
        window = OverviewWindow()
        window.show()
        
        logger.info("Teloude application window launched successfully.")
        
        # --- Initial Startup Checks (Demonstration) ---
        # Simulate reading some storage data for the overview screen
        dummy_storages = [
            {"name": "Personal Photos", "size": "184 GB"}, 
            {"name": "Work Documents", "size": "42 GB"}
        ]
        window.display_status(dummy_storages)

    except Exception as e:
        logger.critical(f"An unrecoverable error occurred during application startup: {e}")
    finally:
        # Ensure database connection is closed when the app exits
        close_db_connection(db_manager)

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    run_main_application()
    sys.exit(app.exec())