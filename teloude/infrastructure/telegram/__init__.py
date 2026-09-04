# teloude/infrastructure/telegram/__init__.py

"""
This module initializes the Telegram infrastructure layer.
It acts as a container for all Telegram-related components and abstractions.

NOTE: Telethon itself is imported ONLY inside `telethon_client.py`.
Importing this package must never require Telethon to be installed.
"""
from .connection_state import ConnectionStatus
from .session_manager import (
    ITelegramSessionManager,
    TelethonSessionManager,
    DummySessionManager,
    sanitize_phone_number,
)

__all__ = [
    "ConnectionStatus",
    "ITelegramSessionManager",
    "TelethonSessionManager",
    "DummySessionManager",
    "sanitize_phone_number",
]
