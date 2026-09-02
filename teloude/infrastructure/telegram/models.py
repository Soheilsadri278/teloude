# teloude/infrastructure/telegram/models.py

from pydantic import BaseModel, Field
from typing import Optional

class TelegramCredentials(BaseModel):
    """Defines the minimum set of credentials required to connect."""
    api_id: int = Field(..., description="Telegram API ID.")
    api_hash: str = Field(..., description="Telegram API Hash.")
    phone_number: str = Field(..., description="User phone number (E.164 format).")

class SessionKeys(BaseModel):
    """Defines the keys required to maintain a logged-in session."""
    session_file: str = Field(..., description="Local path where Telegram stores its session key.")
    is_valid: bool = True # For checking expiry status

# Note: We intentionally do not store raw tokens here, only identifiers for abstraction.

#(End of file - total 23 lines)