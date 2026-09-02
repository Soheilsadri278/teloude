# teloude/config.py

from pydantic import BaseModel, Field
from typing import Optional
import logging

class AppConfig(BaseModel):
    """
    Centralized application configuration schema.
    Handles application settings, user preferences, and runtime state pointers.
    """
    database_path: str = Field("teloude_data.db", description="SQLite path for local metadata.")
    log_level: str = Field("INFO", description="Default logging verbosity level.")
    app_version: str = Field("0.1.0", description="Current application version.")
    # Future settings can be added here (e.g., api_credentials_key, default_storage_name)

def load_configuration() -> 'AppConfig':
    """Loads configuration from environment variables or a dedicated config file."""
    # For Phase 0, we simply use the defaults defined above, mimicking loading.
    return AppConfig()