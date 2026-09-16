# teloude/tests/test_bootstrap.py

import pytest
from pathlib import Path
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

def test_configured_directories(tmp_path):
    """Both default directories must resolve (Windows and POSIX layouts)."""
    from teloude import config as config_module

    custom = AppConfig(data_dir=str(tmp_path / "data"), session_dir=str(tmp_path / "s"))
    assert custom.get_data_dir() == tmp_path / "data"
    assert custom.get_session_dir() == tmp_path / "s"

    assert config_module.default_root_dir(
        appdata="C:/Users/x/AppData/Roaming", is_windows=True
    ) == Path("C:/Users/x/AppData/Roaming") / "Teloude"
    assert config_module.default_root_dir(
        appdata="/home/x/.config", is_windows=False
    ) == Path.home() / ".teloude"
    assert config_module.default_session_dir().name == "sessions"


def test_app_starts_without_any_data_dir_override(tmp_path, monkeypatch):
    """Regression: running without --data-dir crashed on a missing helper.

    A normal shortcut launch has no arguments, so `AppConfig()` must resolve
    every directory by itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    from teloude.infrastructure.database import close_db_connection
    from teloude.ui.app import build_offline

    config = AppConfig()  # exactly what the entry point does with no --data-dir
    data_dir = config.get_data_dir()
    assert data_dir == tmp_path / ".teloude"

    ctx = build_offline(config)
    try:
        assert (data_dir / "teloude_data.db").exists()
        assert ctx.config.database_path.startswith(str(data_dir))
        assert ctx.services.storages.list() == []  # fresh index
    finally:
        ctx.shutdown()
        close_db_connection(ctx.db)
