# teloude/tests/test_telegram_foundation.py

import pytest
from unittest.mock import MagicMock
from teloude.infrastructure.telegram.models import TelegramCredentials
from teloude.infrastructure.telegram.exceptions import AuthError, RateLimitExceeded
from teloude.infrastructure.telegram.session_manager import ITelegramSessionManager, DummySessionManager
from teloude.infrastructure.telegram.client_interface import ITelegramClient

@pytest.fixture(scope="module")
def mock_credentials() -> TelegramCredentials:
    """Fixture for standard dummy credentials."""
    return TelegramCredentials(api_id=12345, api_hash="dummyhash", phone_number="+98912...")


class TestTelegramExceptions:
    def test_exception_hierarchy():
        with pytest.raises(TeloudeTelegramError):
            # Just ensuring the base class works
            raise ConnectionStateError()

class TestSessionManagement:
    def test_session_manager_abstraction(self, mock_credentials: TelegramCredentials):
        """Test that the session manager uses its defined interface contract."""
        # We must use the dummy implementation for testing purposes here.
        manager = DummySessionManager() 
        
        keys = manager.load_session(mock_credentials)
        assert keys is not None, "Loading credentials should return a SessionKeys object."

    def test_dummy_save_session(self, mock_credentials: TelegramCredentials):
        """Test the save functionality of the dummy manager."""
        manager = DummySessionManager()
        test_keys = MagicMock() # Mocking success for this test
        manager.save_session(test_keys)

class TestTelegramClientInterface:
    def test_client_interface_is_abstract(self):
        """Ensures that the ITelegramClient cannot be instantiated directly."""
        # This test confirms that ITelegramClient is an abstract class.
        from abc import ABCMeta, abstractmethod
        if not isinstance(ITelegramClient, ABCMeta):
            pytest.fail("The Telegram client interface must be defined as an Abstract Base Class.")

    def test_abstract_methods_raise(self, mock_credentials: TelegramCredentials):
        """Tests that core methods raise NotImplementedError when called on the abstract class."""
        # We instantiate a dummy concrete implementation to test method calls.
        class MockClient(ITelegramClient):
            def connect(self) -> None: pass # Dummy pass-through
            def get_status(self) -> ConnectionStatus: return ConnectionStatus.READY
            def find_storage(self, storage_name: str) -> Optional[str]: raise NotImplementedError("Must implement")
            def send_message(self, recipient_id: str, message: str) -> bool: raise NotImplementedError("Must implement")
            # ... other abstract methods must also be overridden

        mock_client = MockClient(mock_credentials)
        with pytest.raises(NotImplementedError):
            mock_client.find_storage("test")
