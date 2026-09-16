# teloude/config.py

from pydantic import BaseModel, Field
from typing import Optional
from pathlib import Path
import os
import logging

logger = logging.getLogger("AppConfig")


def default_root_dir(
    appdata: Optional[str] = None, is_windows: Optional[bool] = None
) -> Path:
    """Root directory for Teloude's per-user data.

    Windows: %APPDATA%/Teloude
    Other OS: ~/.teloude

    Performs no I/O; directories are created on demand. The platform inputs are
    parameters so both branches stay testable from any OS.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if appdata is None:
        appdata = os.environ.get("APPDATA")
    if is_windows and appdata:
        return Path(appdata) / "Teloude"
    return Path.home() / ".teloude"


def default_data_dir() -> Path:
    """Directory holding the database, logs and preview cache."""
    return default_root_dir()


def default_session_dir() -> Path:
    """Returns the platform-appropriate default directory for Telegram session files.

    Windows: %APPDATA%/Teloude/sessions
    Other OS: ~/.teloude/sessions

    Performs no I/O; the directory is created on demand by the session manager.
    """
    return default_root_dir() / "sessions"


class AppConfig(BaseModel):
    """
    Centralized application configuration schema.
    Handles application settings, user preferences, and runtime state pointers.
    """
    database_path: str = Field("teloude_data.db", description="SQLite path for local metadata.")
    log_level: str = Field("INFO", description="Default logging verbosity level.")
    app_version: str = Field("0.1.0", description="Current application version.")
    session_dir: Optional[str] = Field(
        None,
        description="Directory for Telegram session files. None means use default_session_dir().",
    )
    data_dir: Optional[str] = Field(
        None,
        description="Application data directory. None means use default_data_dir().",
    )
    speed_limit_mbps: Optional[float] = Field(
        None,
        description="Transfer speed limit in MB/s. None means unlimited.",
    )
    chunk_size_kb: int = Field(
        512, description="Upload/download chunk size in KiB."
    )
    # Future settings can be added here (e.g., api_credentials_key, default_storage_name)

    def get_session_dir(self) -> Path:
        """Resolves the effective session directory (custom or platform default)."""
        if self.session_dir:
            return Path(self.session_dir).expanduser()
        return default_session_dir()

    def get_data_dir(self) -> Path:
        """Resolves the effective application data directory."""
        if self.data_dir:
            return Path(self.data_dir).expanduser()
        return default_data_dir()

    def get_speed_limit_bps(self) -> Optional[int]:
        """Speed limit in bytes/sec, or None for unlimited."""
        if self.speed_limit_mbps is None:
            return None
        return int(self.speed_limit_mbps * 1024 * 1024)


def load_configuration() -> 'AppConfig':
    """Loads configuration from environment variables or a dedicated config file."""
    # For Phase 1.x, we simply use the defaults defined above, mimicking loading.
    return AppConfig()


# --------------------------------------------------------------------------
# Telegram API credentials
#
# Supplied at runtime through the environment (spec section 11: Teloude uses its
# own registered application credentials; the user is not asked for them). The
# values are deliberately never hard-coded, never written to the database or the
# logs, and never committed - a real api_hash in the repository would be a
# leaked credential, and an api_id of 0 is the explicit "not configured" state.
# --------------------------------------------------------------------------

def _api_id_from_env(name: str = "TELOUDE_API_ID") -> int:
    """Reads an integer credential from the environment; 0 means unset/invalid."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        # The variable name is safe to log; its value is not.
        logger.warning("%s is not a valid number; treating it as unset.", name)
        return 0


def _api_hash_from_env(name: str = "TELOUDE_API_HASH") -> str:
    """Reads the api_hash from the environment; empty means unset."""
    return (os.environ.get(name) or "").strip()


TELEGRAM_API_ID: int = _api_id_from_env()
TELEGRAM_API_HASH: str = _api_hash_from_env()
