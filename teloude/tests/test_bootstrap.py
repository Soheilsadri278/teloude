# teloude/tests/test_bootstrap.py

import pytest
from pathlib import Path
import sqlite3
import os
from teloude.config import AppConfig
from teloude.infrastructure.database import DatabaseManager, close_db_connection

@pytest.fixture(scope="function")
def config() -> AppConfig:
    """Provides a fresh configuration instance for testing."""
    return AppConfig()

@pytest.fixture(scope="function", autouse=True)
def setup_db(config: AppConfig):
    """Sets up and tears down the database connection for each test."""
    db_path = config.database_path
    # Clean up before running tests
    if os.path.exists(db_path):
        os.remove(db_path)
    
    manager = DatabaseManager(config)
    if not manager.initialize():
         pytest.skip("Could not initialize database for testing.")
    
    yield manager

    # Teardown: Close connection and remove the test database file
    close_db_connection(manager)
    if os.path.exists(db_path):
        os.remove(db_path)


def test_config_loading():
    """Test if configuration loads correctly (smoke test)."""
    config = AppConfig()
    assert config.database_path == "teloude_data.db"

def test_initial_schema_migration(setup_db: DatabaseManager):
    """Tests the database migration to ensure required tables exist."""
    # The setup fixture already ran migrations, so we check for existence.
    cursor = setup_db.connection.cursor()
    tables = [table[0] for table in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    
    assert "storages" in tables, "The 'storages' table must exist."
    assert "files" in tables, "The 'files' table must exist."
    assert "transfers" in tables, "The 'transfers' table must exist."
    assert "schema_migrations" in tables, "The migration tracking table must exist."

def test_db_write_and_read(setup_db: DatabaseManager):
    """Tests basic CRUD operations."""
    try:
        # Write a record
        storage_id = setup_db.execute_query("INSERT INTO storages (name, total_size) VALUES (?, ?)", ("Test Storage", 100.5))
        assert storage_id > 0
        
        # Read the record
        results = setup_db.execute_query("SELECT name FROM storages WHERE id=?", (storage_id,), fetch=True)
        assert results and results[0][0] == "Test Storage"

    finally:
        close_db_connection(setup_db)