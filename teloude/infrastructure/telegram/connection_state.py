# teloude/infrastructure/telegram/connection_state.py

from enum import Enum
import logging

logger = logging.getLogger("ConnectionState")

class ConnectionStatus(Enum):
    """Defines the possible states of the Telegram client connection."""
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting..."
    AUTHENTICATING = "Authenticating (Waiting for Code)..."
    READY = "Connected and Ready."
    ERROR = "Error State: Check Logs."

def get_status_display(status: ConnectionStatus) -> str:
    """Returns a user-friendly display string for the status."""
    return status.value

# Status change helper functions can be added here later (e.g., attempt_reconnect())

#(End of file - total 24 lines)