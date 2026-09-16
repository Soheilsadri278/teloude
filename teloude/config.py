# teloude/config.py

from pydantic import BaseModel, Field
from typing import Optional, Tuple
from pathlib import Path
import json
import os
import logging
import sys

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
#
# A release build also bakes the credentials into the bundle (spec section 11:
# "the user should not normally be required to manually enter Telegram API
# credentials", and a user double-clicking a shortcut has no environment to set).
# scripts/build_windows.ps1 writes them to installer/build_credentials.json,
# teloude.spec ships that file inside the bundle, and the file is gitignored, so
# the values still never enter the repository or the history.
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


BUILD_CREDENTIALS_FILENAME = "build_credentials.json"


def build_credentials_path() -> Optional[Path]:
    """Where a frozen release build looks for its baked-in credentials.

    Returns None when the application is not running from a bundle: a source
    checkout must be configured through the environment, so a developer machine
    can never silently talk to Telegram as the release application.
    """
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = Path(sys.executable).parent
    return Path(base) / BUILD_CREDENTIALS_FILENAME


def _credentials_from_build(path: Optional[Path] = None) -> Tuple[int, str]:
    """Reads the credentials baked in at build time; (0, "") when unavailable.

    Never logs the values - only whether the file could be used (spec section 11:
    API credentials must never be written to logs).
    """
    if path is None:
        path = build_credentials_path()
    if path is None:
        return 0, ""
    try:
        if not Path(path).is_file():
            return 0, ""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable build credentials (%s).", type(exc).__name__)
        return 0, ""

    api_id = raw.get("api_id") if isinstance(raw, dict) else None
    api_hash = raw.get("api_hash") if isinstance(raw, dict) else None
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        return 0, ""
    api_hash = str(api_hash or "").strip()
    if not api_id or not api_hash:
        return 0, ""
    logger.info("Using the Telegram API credentials baked into this build.")
    return api_id, api_hash


def _resolve_api_credentials() -> Tuple[int, str]:
    """Environment first (development, tests, support overrides), then the build."""
    api_id = _api_id_from_env()
    api_hash = _api_hash_from_env()
    if api_id and api_hash:
        return api_id, api_hash
    build_id, build_hash = _credentials_from_build()
    return api_id or build_id, api_hash or build_hash


TELEGRAM_API_ID, TELEGRAM_API_HASH = _resolve_api_credentials()
