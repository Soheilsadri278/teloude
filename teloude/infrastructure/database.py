# teloude/infrastructure/database.py

import sqlite3
from typing import Any
from .config import AppConfig
import logging

logger = logging.getLogger("DatabaseManager")

class DatabaseManager:
    """
    Handles all SQLite connection and schema management. 
    Centralizes database access, fulfilling the infrastructure layer role.
    """
    def __init__(self, config: AppConfig):
        self._db_path = config.database_path
        self.connection = None

    def connect(self) -> None:
        """Establishes the SQLite connection."""
        try:
            self.connection = sqlite3.connect(self._db_path, check_same_thread=False)
            logger.info(f"Successfully connected to database at {self._db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def execute_query(self, query: str, params: tuple = (), fetch: bool = False) -> Any:
        """Helper method to execute read/write queries."""
        if not self.connection:
            raise ConnectionError("Database connection is not established.")
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            if fetch:
                return cursor.fetchall()
            self.connection.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"SQL Error executing '{query[:50]}...': {e}")
            raise

    def initialize(self) -> bool:
        """Initializes the DB connection and runs all necessary migrations."""
        try:
            self.connect()
            self.run_migrations()
            return True
        except Exception as e:
            logger.critical(f"Database initialization failed critically: {e}")
            return False

    def run_migrations(self) -> None:
        """Runs the migration logic."""
        # This simulates checking for and applying schema changes based on a version table
        MIGRATION_TABLE = "schema_migrations"
        VERSION_KEY = "version"

        # 1. Check if the migration tracking table exists
        try:
            self.execute_query(f"SELECT {VERSION_KEY} FROM sqlite_master WHERE type='table' AND name=?", (MIGRATION_TABLE,))
        except Exception:
             # Table likely doesn't exist, proceed with creation
            logger.warning("Migration tracking table not found. Creating it.")
            self.execute_query(f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ({VERSION_KEY} INTEGER PRIMARY KEY)")

        # 2. Check current version (placeholder logic)
        try:
            current_version = self.execute_query(f"SELECT {VERSION_KEY} FROM {MIGRATION_TABLE} ORDER BY {VERSION_KEY} DESC LIMIT 1", fetch=True)[0][0]
        except Exception:
            # No migrations found yet, start at version 0
            current_version = 0
            logger.info("No existing migrations found. Starting from Version 0.")

        if current_version == 0:
            print("\n*** Running Initial Database Migration (v1) ***")
            self._migrate_v1()
            # Mark migration as complete
            self.execute_query(f"INSERT INTO {MIGRATION_TABLE} ({VERSION_KEY}) VALUES (?)", (1,))
            logger.info("Migration to Version 1 completed successfully.")

    def _migrate_v1(self) -> None:
        """Applies all necessary schema changes for the initial version."""
        # Storage Metadata Table
        create_storage_table = """
        CREATE TABLE IF NOT EXISTS storages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            total_size REAL DEFAULT 0.0,
            file_count INTEGER DEFAULT 0,
            last_backup_date TEXT
        );"""
        self.execute_query(create_storage_table)

        # File Metadata Table (Local index of backed-up files)
        create_files_table = """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_id INTEGER REFERENCES storages(id),
            local_path TEXT UNIQUE NOT NULL,
            relative_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            sha256_hash TEXT,
            is_backed_up BOOLEAN DEFAULT 0,
            FOREIGN KEY (storage_id) REFERENCES storages(id)
        );"""
        self.execute_query(create_files_table)

        # Transfer State Table (For crash recovery)
        create_transfers_table = """
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER REFERENCES files(id),
            storage_id INTEGER REFERENCES storages(id),
            status TEXT NOT NULL DEFAULT 'queued', 
            uploaded_bytes BLOB DEFAULT 0,
            last_error TEXT,
            UNIQUE (file_id)
        );"""
        self.execute_query(create_transfers_table)

# Helper function to ensure clean closure
def close_db_connection(manager: DatabaseManager):
    if manager and manager.connection:
        manager.connection.close()
        logger.info("Database connection closed.")