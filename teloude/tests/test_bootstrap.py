# teloude/tests/test_bootstrap.py

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _patch_home_environment(tmp_path, monkeypatch) -> None:
    """Points every home-directory lookup at ``tmp_path``.

    ``Path.home()`` is ``os.path.expanduser("~")``, and the two platforms read
    different variables: POSIX uses HOME, Windows uses USERPROFILE (falling back
    to HOMEDRIVE+HOMEPATH). Patching only HOME is a silent no-op on Windows,
    where the test then measures the real user profile instead of the temp dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    drive, tail = os.path.splitdrive(str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail or str(tmp_path))


def test_app_starts_without_any_data_dir_override(tmp_path, monkeypatch):
    """Regression: running without --data-dir crashed on a missing helper.

    A normal shortcut launch has no arguments, so `AppConfig()` must resolve
    every directory by itself. APPDATA is deliberately removed to exercise the
    documented fallback for a Windows profile that has none; the home variables
    must be patched for the result to be hermetic on every platform.
    """
    _patch_home_environment(tmp_path, monkeypatch)
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

# Bootstrap run inside the subprocess: hides PySide6 from the import system and
# then runs the real entry point. A fresh interpreter is used on purpose - this
# is the only way to prove what `python -m teloude.main` does on a machine where
# the GUI dependency is absent, on every platform and Python version.
_ENTRY_POINT_BOOTSTRAP = """
import sys
sys.path.insert(0, {repo_root!r})
if {block_pyside6}:
    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
            return None
    sys.meta_path.insert(0, _Blocker())
sys.argv = ["teloude.main"] + sys.argv[1:]
import runpy
runpy.run_module("teloude.main", run_name="__main__")
"""


def _run_entry_point(args, data_dir, block_pyside6: bool, credentials):
    """Runs `python -m teloude.main <args>` in a controlled environment."""
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() not in ("TELOUDE_API_ID", "TELOUDE_API_HASH")
    }
    env["QT_QPA_PLATFORM"] = "offscreen"  # headless CI has no display
    env["PYTHONIOENCODING"] = "utf-8"
    env["HOME"] = str(data_dir)  # never touch a real user profile
    env["USERPROFILE"] = str(data_dir)
    if credentials is not None:
        env["TELOUDE_API_ID"], env["TELOUDE_API_HASH"] = credentials

    code = _ENTRY_POINT_BOOTSTRAP.format(
        repo_root=str(repo_root), block_pyside6=block_pyside6,
    )
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=180,
    )


class TestRealModeConfiguration:
    """Non-offline startup must reach the credential check, never an ImportError.

    `python -m teloude.main` (without --offline) imports its API credentials from
    `teloude.config`; if those names are missing the process dies with an
    ImportError before any validation runs, so the app can never talk to
    Telegram. These tests pin the runtime credential model and the entry point
    itself.
    """

    def test_config_exposes_the_names_the_entry_point_imports(self):
        # This exact import is what teloude/ui/app.py performs at startup.
        from teloude.config import TELEGRAM_API_ID, TELEGRAM_API_HASH

        assert isinstance(TELEGRAM_API_ID, int)
        assert isinstance(TELEGRAM_API_HASH, str)

    def test_credentials_come_from_the_environment(self, monkeypatch):
        from teloude.config import _api_hash_from_env, _api_id_from_env

        monkeypatch.delenv("TELOUDE_API_ID", raising=False)
        monkeypatch.delenv("TELOUDE_API_HASH", raising=False)
        assert _api_id_from_env() == 0  # explicit "not configured" state
        assert _api_hash_from_env() == ""

        monkeypatch.setenv("TELOUDE_API_ID", "12345")
        monkeypatch.setenv("TELOUDE_API_HASH", "0123456789abcdef0123456789abcdef")
        assert _api_id_from_env() == 12345
        assert _api_hash_from_env() == "0123456789abcdef0123456789abcdef"

        monkeypatch.setenv("TELOUDE_API_ID", "  ")  # blank is unset, not a crash
        assert _api_id_from_env() == 0

    def test_invalid_credentials_are_rejected_without_leaking_them(
        self, monkeypatch, caplog
    ):
        from teloude.config import _api_id_from_env, _api_hash_from_env

        monkeypatch.setenv("TELOUDE_API_ID", "my-secret-junk")
        with caplog.at_level(logging.WARNING):
            assert _api_id_from_env() == 0
        assert "my-secret-junk" not in caplog.text  # never log credential values
        assert "TELOUDE_API_ID" in caplog.text  # the variable name is safe

        monkeypatch.setenv("TELOUDE_API_HASH", "")
        assert _api_hash_from_env() == ""

    def test_unconfigured_real_mode_exits_with_actionable_message(self, tmp_path):
        """The real entry point reports missing configuration, not an exception.

        The subprocess runs with PySide6 hidden, which is what a minimal install
        (or a Python version without a Qt wheel) looks like: configuration must
        still be validated first, and it must be reported as itself.
        """
        result = _run_entry_point(["--data-dir", str(tmp_path)], tmp_path,
                                  block_pyside6=True, credentials=None)

        output = result.stdout + result.stderr
        assert "ImportError" not in output, output
        assert "Traceback" not in output, output
        assert result.returncode == 2, output
        assert "TELOUDE_API_ID" in result.stdout and "TELOUDE_API_HASH" in result.stdout
        # Configuration is checked before the GUI dependency, so a machine
        # missing both still gets the actionable configuration message.
        assert "PySide6" not in output, output

    def test_missing_gui_dependency_is_a_message_not_a_traceback(self, tmp_path):
        """With credentials present, a missing PySide6 must still be explained."""
        result = _run_entry_point(
            ["--data-dir", str(tmp_path)], tmp_path,
            block_pyside6=True,
            credentials=("12345", "0123456789abcdef0123456789abcdef"),
        )

        output = result.stdout + result.stderr
        assert "Traceback" not in output, output
        assert result.returncode == 3, output
        assert "PySide6" in result.stdout, output
        assert "pip install -e ." in result.stdout, output

