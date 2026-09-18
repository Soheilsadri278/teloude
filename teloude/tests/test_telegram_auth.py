# teloude/tests/test_telegram_auth.py
"""Authentication flow tests: states, 2FA branch, error mapping, no secret logging."""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teloude.infrastructure.telegram.auth import AuthState, ITelegramAuth, TelethonAuth
from teloude.infrastructure.telegram.exceptions import AuthError
from teloude.infrastructure.telegram.fakes import FakeAuth


@pytest.fixture()
def authorized_client():
    client = MagicMock()
    client.send_code_request.return_value = SimpleNamespace(phone_code_hash="hash1")
    client.sign_in.return_value = SimpleNamespace(id=1)
    client.is_user_authorized.return_value = True
    return client


class TestFakeAuth:
    def test_simple_flow(self):
        auth = FakeAuth()
        assert auth.send_code("+1000") is AuthState.CODE_SENT
        assert auth.sign_in_code("+1000", "11111") is AuthState.AUTHORIZED
        assert auth.is_authorized() is True
        auth.sign_out()
        assert auth.is_authorized() is False

    def test_two_factor_flow(self):
        auth = FakeAuth(needs_password=True)
        auth.send_code("+1000")
        assert auth.sign_in_code("+1000", "11111") is AuthState.PASSWORD_NEEDED
        assert auth.is_authorized() is False
        assert auth.sign_in_password("secret") is AuthState.AUTHORIZED

    def test_wrong_code_and_password(self):
        auth = FakeAuth(needs_password=True)
        auth.send_code("+1000")
        with pytest.raises(AuthError):
            auth.sign_in_code("+1000", "00000")
        auth.sign_in_code("+1000", "11111")
        with pytest.raises(AuthError):
            auth.sign_in_password("nope")


class TestTelethonAuth:
    def test_code_flow(self, authorized_client):
        auth = TelethonAuth(authorized_client)
        assert isinstance(auth, ITelegramAuth)
        assert auth.send_code("+1000") is AuthState.CODE_SENT
        authorized_client.send_code_request.assert_called_once_with("+1000")
        assert auth.sign_in_code("+1000", "12345") is AuthState.AUTHORIZED
        assert auth.session is not None and auth.session.state is AuthState.AUTHORIZED

    def test_password_needed_branch(self):
        from telethon.errors import SessionPasswordNeededError

        client = MagicMock()
        client.send_code_request.return_value = SimpleNamespace(phone_code_hash="h")
        client.sign_in.side_effect = SessionPasswordNeededError(request=MagicMock())
        auth = TelethonAuth(client)
        auth.send_code("+1000")
        assert auth.sign_in_code("+1000", "12345") is AuthState.PASSWORD_NEEDED
        client.sign_in.assert_called_with("+1000", "12345", phone_code_hash="h")

    def test_invalid_code_maps_to_auth_error(self):
        from telethon.errors import PhoneCodeInvalidError

        client = MagicMock()
        client.send_code_request.return_value = SimpleNamespace(phone_code_hash="h")
        client.sign_in.side_effect = PhoneCodeInvalidError(request=MagicMock())
        auth = TelethonAuth(client)
        auth.send_code("+1000")
        with pytest.raises(AuthError, match="incorrect"):
            auth.sign_in_code("+1000", "00000")

    def test_password_sign_in(self, authorized_client):
        auth = TelethonAuth(authorized_client)
        assert auth.sign_in_password("secret") is AuthState.AUTHORIZED
        authorized_client.sign_in.assert_called_once_with(password="secret")

    def test_sign_out(self, authorized_client):
        auth = TelethonAuth(authorized_client)
        auth.send_code("+1000")
        auth.sign_out()
        assert auth.session is None
        authorized_client.log_out.assert_called_once()

    def test_is_authorized_passthrough(self, authorized_client):
        assert TelethonAuth(authorized_client).is_authorized() is True

    def test_secrets_never_logged(self, authorized_client, caplog):
        auth = TelethonAuth(authorized_client)
        with caplog.at_level(logging.INFO, logger="TelegramAuth"):
            auth.send_code("+1000")
            auth.sign_in_code("+1000", "SUPERSECRET123")
            auth.sign_in_password("HUNTER2")
        unclean = " ".join(r.getMessage() for r in caplog.records)
        assert "SUPERSECRET123" not in unclean
        assert "HUNTER2" not in unclean
