# teloude/infrastructure/telegram/client_interface.py

from abc import ABC, abstractmethod
from typing import Optional, List
import logging
from .exceptions import TeloudeTelegramError, ConnectionStateError, RateLimitExceeded
from .models import TelegramCredentials
from .connection_state import ConnectionStatus # Using the state enum

logger = logging.getLogger("ITelegramClient")

class ITelegramClient(ABC):
    """
    ABSTRACT INTERFACE: The primary contract for interacting with Telegram.
    The core application must only interact with this interface, ensuring total decoupling 
    from any underlying library (Telethon/Pyrogram).
    """

    def __init__(self, credentials: TelegramCredentials):
        self._credentials = credentials

    @abstractmethod
    def connect(self) -> None:
        """
        Initiates the connection process. This method should handle authentication flow 
        and update the internal connection state. Must raise ConnectionStateError on failure.
        """
        pass

    @abstractmethod
    def get_status(self) -> ConnectionStatus:
        """Returns the current operational status of the client."""
        return ConnectionStatus.DISCONNECTED # Placeholder implementation

    # --- Business Methods (Abstract Placeholders Only) ---

    @abstractmethod
    def find_storage(self, storage_name: str) -> Optional[str]:
        """
        Searches Telegram for a specific Storage name and returns its ID/identifier 
        if it exists. This is purely abstract.
        """
        pass

    @abstractmethod
    def send_message(self, recipient_id: str, message: str) -> bool:
        """Sends a simple test message to ensure connectivity."""
        pass
    
    # --- Core Functionality Placeholders (DO NOT IMPLEMENT YET) ---
    # این بخش‌ها کاملاً مجرد هستند و نباید در فاز ۱ فعال شوند.

    @abstractmethod
    def upload_file(self, local_path: str, target_id: str) -> None:
        """Handles large file uploads with chunking and resume logic."""
        raise NotImplementedError("File Upload not implemented in this phase.")

    @abstractmethod
    def get_user_messages(self, topic_id: str, count: int = 10) -> List[dict]:
        """Fetches messages for a given Topic ID."""
        raise NotImplementedError("Fetching Messages not implemented in this phase.")


(End of file - total 146 lines)