# teloude/tests/test_session_auth.py
"""Phase 1.3 tests: Telegram session & authentication foundation.

All tests use fakes/mocks. No real Telegram account, credentials, or
network access is required. Deterministic: no sleeps, no randomness.
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teloude.config import AppConfig, default_session_dir
from teloude.infrastructure.telegram.models import TelegramCredentials, SessionKeys
from teloude.infrastructure.telegram.connection_state import ConnectionStatus
from teloude.infrastructure.telegram.exceptions import (
    ConnectionStateError,
    RateLimitExceeded,
    SessionError,
)
from teloude.infrastructure.telegram.session_manager import (
    ITelegramSessionManager,
    TelethonSessionManager,
    DummySessionManager,
    sanitize_phone_number,
)
from teloude.infrastructure.telegram.client_interface import ITelegramClient
from teloude.infrastructure.telegram import telethon_client as tc_module
from teloude.infrastructure.telegram import bridge as tc_module_bridge
from teloude.infrastructure.telegram.telethon_client import TelethonTelegramClient


# Reserved fictional number (ITU 555 test range): no real subscriber.
PHONE = "+15005550006"
PHONE_DIGITS = "15005550006"


@pytest.fixture(scope="module")
def creds() -> TelegramCredentials:
    return TelegramCredentials(api_id=12345, api_hash="dummyhash", phone_number=PHONE)


def make_fake_low_level(authorized: bool = True, fail_connect: bool = False) -> MagicMock:
    """Sync fake mirroring the Telethon surface our adapter uses."""
    fake = MagicMock()
    if fail_connect:
        fake.connect.side_effect = OSError("no route to host")
    else:
        fake.connect.return_value = None
    fake.is_user_authorized.return_value = authorized
    fake.send_message.return_value = MagicMock()
    fake.disconnect.return_value = None
    return fake


def make_client(creds, tmp_path, low_level=None, **kwargs):
    manager = TelethonSessionManager(session_dir=tmp_path)
    factory = lambda session, api_id, api_hash: low_level  # noqa: E731
    return TelethonTelegramClient(
        creds, session_manager=manager, client_factory=factory, **kwargs
    ), manager


class TestSessionManagerInit:
    def test_default_dir_is_configurable_location(self):
        manager = TelethonSessionManager()
        assert isinstance(manager.session_dir, Path)
        assert manager.session_dir == default_session_dir()

    def test_custom_dir_is_honored(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path / "custom")
        assert manager.session_dir == tmp_path / "custom"

    def test_app_config_session_dir(self, tmp_path):
        cfg = AppConfig(session_dir=str(tmp_path / "sessions"))
        assert cfg.get_session_dir() == tmp_path / "sessions"
        assert AppConfig().get_session_dir() == default_session_dir()

    def test_implements_abstraction(self, tmp_path):
        assert isinstance(TelethonSessionManager(session_dir=tmp_path), ITelegramSessionManager)
        assert isinstance(DummySessionManager(), ITelegramSessionManager)


class TestSessionPathHandling:
    def test_path_shape_and_location(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        path = manager.get_session_path(PHONE)
        assert path.parent == tmp_path
        assert path.suffix == ".session"
        assert path.name == f"{PHONE_DIGITS}.session"

    def test_formatting_variants_resolve_to_same_path(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        assert manager.get_session_path("+1 500-555 0006") == manager.get_session_path(PHONE)

    def test_different_numbers_differ(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        assert manager.get_session_path("+15005550006") != manager.get_session_path("+15005559999")

    def test_invalid_phone_rejected(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        with pytest.raises(SessionError):
            manager.get_session_path("not-a-number")
        with pytest.raises(SessionError):
            sanitize_phone_number("../../etc/passwd")

    def test_no_hardcoded_session_name(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        assert "teloude_session" not in str(manager.get_session_path(PHONE))


class TestSessionPersistence:
    def test_missing_session_returns_none(self, tmp_path, creds):
        manager = TelethonSessionManager(session_dir=tmp_path)
        assert manager.session_exists(PHONE) is False
        assert manager.load_session(creds) is None

    def test_existing_session_detection(self, tmp_path, creds):
        manager = TelethonSessionManager(session_dir=tmp_path)
        expected = manager.get_session_path(PHONE)
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.touch()
        assert manager.session_exists(PHONE) is True
        keys = manager.load_session(creds)
        assert isinstance(keys, SessionKeys)
        assert keys.session_file == str(expected)
        assert keys.is_valid is True

    def test_save_prepares_directory_without_writing_secrets(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path / "nested" / "sessions")
        target = tmp_path / "nested" / "sessions" / f"{PHONE_DIGITS}.session"
        manager.save_session(SessionKeys(session_file=str(target)))
        assert target.parent.is_dir()
        # save_session only prepares the location; Telethon owns file contents.
        assert target.is_file() is False

    def test_save_rejects_empty_path(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        with pytest.raises(SessionError):
            manager.save_session(SessionKeys(session_file=""))

    def test_clear_session(self, tmp_path):
        manager = TelethonSessionManager(session_dir=tmp_path)
        path = manager.get_session_path(PHONE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        manager.clear_session(PHONE)
        assert path.exists() is False
        # Clearing a missing session is not an error (idempotent logout).
        manager.clear_session(PHONE)


class TestClientInitialization:
    def test_initial_status_disconnected(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        assert client.get_status() is ConnectionStatus.DISCONNECTED

    def test_session_path_uses_manager_dir(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        assert client.session_path.parent == tmp_path
        assert "teloude_session" not in str(client.session_path)

    def test_explicit_session_path_override(self, tmp_path, creds):
        custom = str(tmp_path / "override" / "login.session")
        client = TelethonTelegramClient(
            creds, session_path=custom, client_factory=lambda *a: make_fake_low_level()
        )
        assert client.session_path == Path(custom)

    def test_is_itelegram_client(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        assert isinstance(client, ITelegramClient)


class TestConnectionStateBehavior:
    def test_successful_connect_sets_ready(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level(authorized=True))
        client.connect()
        assert client.get_status() is ConnectionStatus.READY

    def test_unauthorized_session_sets_authenticating(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level(authorized=False))
        client.connect()
        assert client.get_status() is ConnectionStatus.AUTHENTICATING

    def test_network_failure_sets_error(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level(fail_connect=True))
        with pytest.raises(ConnectionStateError):
            client.connect()
        assert client.get_status() is ConnectionStatus.ERROR

    def test_async_low_level_client_supported(self, tmp_path, creds):
        class AsyncFake:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def disconnect(self):
                return None

        client, _ = make_client(creds, tmp_path, AsyncFake())
        client.connect()
        assert client.get_status() is ConnectionStatus.READY

    def test_disconnect_returns_to_disconnected(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        client.connect()
        client.disconnect()
        assert client.get_status() is ConnectionStatus.DISCONNECTED

    def test_flood_wait_mapped_to_rate_limit(self, tmp_path, creds):
        from telethon.errors import FloodWaitError

        fake = make_fake_low_level()
        fake.connect.side_effect = FloodWaitError(MagicMock(), 42)
        client, _ = make_client(creds, tmp_path, fake)
        with pytest.raises(RateLimitExceeded) as exc_info:
            client.connect()
        assert exc_info.value.retry_after == 42
        assert client.get_status() is ConnectionStatus.ERROR

    def test_missing_telethon_reported_clearly(self, tmp_path, creds, monkeypatch):
        monkeypatch.setattr(tc_module, "TelegramClient", None)
        monkeypatch.setattr(tc_module, "_TELETHON_AVAILABLE", False)
        client = TelethonTelegramClient(creds, session_path=str(tmp_path / "x.session"))
        assert client.get_status() is ConnectionStatus.DISCONNECTED
        with pytest.raises(ConnectionStateError, match="not installed"):
            client.connect()
        assert client.get_status() is ConnectionStatus.ERROR


class TestClientErrorHandling:
    def test_send_requires_ready(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        with pytest.raises(ConnectionStateError):
            client.send_message("me", "hello")

    def test_send_after_connect(self, tmp_path, creds):
        fake = make_fake_low_level()
        client, _ = make_client(creds, tmp_path, fake)
        client.connect()
        assert client.send_message("me", "hello") is True
        fake.send_message.assert_called_once()

    def test_placeholders_stay_explicit(self, tmp_path, creds):
        client, _ = make_client(creds, tmp_path, make_fake_low_level())
        with pytest.raises(NotImplementedError):
            client.upload_file("a.bin", "target")
        with pytest.raises(NotImplementedError):
            client.get_user_messages("topic")


class TestAbstractionBoundaries:
    LEAF_MODULES = [
        "client_interface.py",
        "session_manager.py",
        "models.py",
        "connection_state.py",
        "exceptions.py",
    ]

    def test_no_telethon_leak_outside_adapter(self):
        # Boundary = no Telethon *imports* outside the adapter. Prose mentions
        # in docstrings (e.g. "decoupled from Telethon/Pyrogram") are fine.
        import re

        base = Path(__file__).resolve().parent.parent / "infrastructure" / "telegram"
        pattern = re.compile(r"^\s*(from|import)\s+telethon", re.MULTILINE)
        for name in self.LEAF_MODULES:
            source = (base / name).read_text(encoding="utf-8")
            assert not pattern.search(source), f"Telethon import leak in {name}"

    def test_adapter_owns_telethon_import(self):
        base = Path(__file__).resolve().parent.parent / "infrastructure" / "telegram"
        source = (base / "telethon_client.py").read_text(encoding="utf-8")
        assert "telethon" in source.lower()

    def test_running_coroutine_inside_loop_rejected(self):
        # Only the Telegram loop thread itself is rejected (would deadlock);
        # any other thread - even one running an unrelated loop - is served.
        async def main():
            return tc_module._resolve(_value())

        async def _value():
            await asyncio.sleep(0)
            return "served"

        assert asyncio.run(main()) == "served"

    def test_run_sync_on_telegram_loop_thread_rejected(self):
        async def probe():
            with pytest.raises(ConnectionStateError):
                tc_module._resolve(_value())

        async def _value():
            return "unreachable"

        loop = tc_module_bridge.get_telegram_loop()
        outcome = asyncio.run_coroutine_threadsafe(probe(), loop).result(timeout=10)
        assert outcome is None
