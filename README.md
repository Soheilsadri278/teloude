# Teloude Project Boilerplate

This repository bootstraps the foundational structure for a professional desktop backup application using Python, PySide6, SQLite, and structured logging. It adheres strictly to Phase 0 requirements by avoiding all Telegram-related implementations or speculative features.

## Structure Overview

The architecture follows clear separation of concerns:
*   `ui/`: Contains all PySide6 components (Views, Dialogs, Models). Must not contain business logic.
*   `app/core/`: Core Domain Logic (e.g., `BackupManager`, `FileScanner`). Business logic independent of UI/DB/Net.
*   `app/infrastructure/`: Low-level implementation details (e.g., DB layer, Config loading).
*   `tests/`: Unit and integration tests.

## Setup Instructions

1.  **Virtual Environment:** Create a virtual environment (`python -m venv venv`).
2.  **Install Dependencies:** Install required packages listed in `pyproject.toml`.
3.  **Run Initialization Script:** Execute the main application entry point to perform setup and migrations.
    `python run_app.py`

## Project Phases

This project development follows a phased approach:
- **Phase 0 (Current): Bootstrap.** Establishes the minimal runnable foundation.
- **Phase 1:** Telegram Foundation & Authentication.
- **Phase 2+**: Advanced features (Scanning, Backup, Restore).

---
*Note: The core logic is designed to be tested and expanded systematically.*