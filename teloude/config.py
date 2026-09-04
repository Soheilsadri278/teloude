# teloude/config.py

from pydantic import BaseModel, Field
from typing import Optional
from pathlib import Path
import os
import logging

logger = logging.getLogger("AppConfig")


def default_session_dir() -> Path:
    """Returns the platform-appropriate default directory for Telegram session files.

    Windows: %APPDATA%/Teloude/sessions
    Other OS: ~/.teloude/sessions

    Performs no I/O; the directory is created on demand by the session manager.
    """
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / "Teloude" / "sessions"
    return Path.home() / ".teloude" / "sessions"


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
