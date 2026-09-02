from typing import Optional, List
import logging

from telethon import TelegramClient

from .client_interface import ITelegramClient
from .models import TelegramCredentials
from .connection_state import ConnectionStatus
from .exceptions import ConnectionStateError


logger = logging.getLogger("TelethonClient")


class TelethonTelegramClient(ITelegramClient):

    def __init__(self, credentials: TelegramCredentials):
        super().__init__(credentials)

        self._client = TelegramClient(
            "teloude_session",
            credentials.api_id,
            credentials.api_hash
        )

        self._status = ConnectionStatus.DISCONNECTED


    def connect(self) -> None:
        try:
            self._client.connect()

            self._status = ConnectionStatus.READY

            logger.info("Telegram connected successfully")

        except Exception as e:
            self._status = ConnectionStatus.ERROR
            raise ConnectionStateError(str(e))


    def get_status(self) -> ConnectionStatus:
        return self._status


    def find_storage(self, storage_name: str) -> Optional[str]:
        # Phase 1.2 placeholder
        return None


    def send_message(self, recipient_id: str, message: str) -> bool:
        try:
            self._client.send_message(
                recipient_id,
                message
            )
            return True

        except Exception as e:
            logger.error(e)
            return False


    def upload_file(self, local_path: str, target_id: str) -> None:
        raise NotImplementedError(
            "Upload will be implemented in Phase 1.3"
        )


    def get_user_messages(
        self,
        topic_id: str,
        count: int = 10
    ) -> List[dict]:

        raise NotImplementedError(
            "Message fetching will be implemented later"
        )