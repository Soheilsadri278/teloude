# teloude/infrastructure/telegram/__init__.py

"""
This module initializes the Telegram infrastructure layer.
It acts as a container for all Telegram-related components and abstractions.

NOTE: Telethon itself is imported ONLY inside `telethon_client.py` and lazily
inside gateway methods. Importing this package must never require Telethon
to be installed.
"""
from .connection_state import ConnectionStatus
from .session_manager import (
    ITelegramSessionManager,
    TelethonSessionManager,
    DummySessionManager,
    sanitize_phone_number,
)
from .auth import AuthState, AuthSession, ITelegramAuth, TelethonAuth
from .storage import (
    DialogState,
    DocumentMeta,
    ITelegramStorage,
    StorageInfo,
    TelethonStorageGateway,
    TopicInfo,
    telethon_list_dialogs,
)
from .files import (
    DocumentRef,
    ITelegramFileGateway,
    SentMessage,
    TelethonFileGateway,
    UploadCancelled,
    UploadedFile,
    UploadPaused,
)

__all__ = [
    "ConnectionStatus",
    "ITelegramSessionManager",
    "TelethonSessionManager",
    "DummySessionManager",
    "sanitize_phone_number",
    "AuthState",
    "AuthSession",
    "ITelegramAuth",
    "TelethonAuth",
    "DialogState",
    "DocumentMeta",
    "ITelegramStorage",
    "StorageInfo",
    "TelethonStorageGateway",
    "TopicInfo",
    "telethon_list_dialogs",
    "DocumentRef",
    "ITelegramFileGateway",
    "SentMessage",
    "TelethonFileGateway",
    "UploadCancelled",
    "UploadedFile",
    "UploadPaused",
]
