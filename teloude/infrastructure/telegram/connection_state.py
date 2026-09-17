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


# The four states the connection indicator speaks (disconnected / connecting /
# connected / error) are defined in connection.py, next to the layer that owns
# them; ``ConnectionState.from_status`` maps this lifecycle onto them.