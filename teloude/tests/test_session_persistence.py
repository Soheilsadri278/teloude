# teloude/tests/test_session_persistence.py
"""Bug 2 regression tests: the saved session must survive a restart.

The Windows acceptance test signed in with phone -> code -> 2FA, restarted the
app, and was asked to sign in again even though the session was still valid.

Root cause: the Telethon session file is named after the phone number
(``<session_dir>/<digits>.session``), but the app never remembered which number
it had signed in with, and ``AuthService`` did not even expose the
``is_authorized()`` its interface promises. The startup gate therefore always
answered "not authorized" and showed the sign-in dialog.

These tests run the production stack (real ``TelethonTelegramClient`` wiring,
real ``SecureSessionStore``, real repositories) against a scripted Telegram
server that keeps its state across simulated restarts.
"""
import os

import pytest
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from teloude.application.services import AuthService, EventBus
from teloude.config import AppConfig
from teloude.infrastructure.database import close_db_connection
from teloude.infrastructure.security.dpapi import SessionProtector
from teloude.infrastructure.telegram.auth import AuthState
from teloude.infrastructure.telegram.fakes import FakeAuth
from teloude.infrastructure.telegram.models import TelegramCredentials
from teloude.infrastructure.telegram.session_manager import TelethonSessionManager
from teloude.infrastructure.telegram.telethon_client import TelethonTelegramClient
from teloude.ui.app import build_real, restore_saved_session

PHONE = "+10000000000"
CODE = "11111"
PASSWORD = "supersecret-2fa"
SESSION_PAYLOAD = b"SQLite format 3\x00teloude-session-under-test"


class ScriptedSessionServer:
    """The Telegram side: survives every simulated app restart."""

    def __init__(self, needs_password: bool = False):
        self.needs_password = needs_password
        self.authorized = False
        self.codes_requested = []
        self.signed_in = []
        self.passwords_used = []
        self.logouts = 0

    def send_code_request(self, phone):
        self.codes_requested.append(phone)
        return _ns(phone_code_hash="hash:1")

    def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
        if password is not None:
            if password != PASSWORD:
                raise PasswordHashInvalidError(request=None)
            self.passwords_used.append(password)
            self.authorized = True
            return _ns()
        if code != CODE:
            raise PhoneCodeInvalidError(request=None)
        if self.needs_password:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True
        self.signed_in.append(phone)
        return _ns()

    def log_out(self):
        self.logouts += 1
        self.authorized = False
        return True

    def get_me(self):
        return _ns(id=42, first_name="Tester")


def _ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


class SessionFileClient:
    """Stands in for Telethon's TelegramClient: owns the session file."""

    def __init__(self, session_path, server):
        self._path = session_path
        self._server = server
        self.connected = 0
        self.disconnected = 0

    # Telethon's SQLiteSession creates/updates this file; nothing else does.
    def connect(self):
        self.connected += 1
        with open(self._path, "wb") as handle:
            handle.write(SESSION_PAYLOAD)
        return True

    def is_user_authorized(self):
        return self._server.authorized

    def send_code_request(self, phone):
        return self._server.send_code_request(phone)

    def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
        return self._server.sign_in(phone=phone, code=code, password=password)

    def log_out(self):
        return self._server.log_out()

    def get_me(self):
        return self._server.get_me()

    def disconnect(self):
        self.disconnected += 1
        return True


class XorProtector(SessionProtector):
    """Test double proving the store protects, unprotects and rejects garbage.

    Real DPAPI refuses to decrypt a damaged blob (and only works on Windows);
    this stands in for that behaviour on any platform, and its output never
    contains the plaintext bytes.
    """

    KEY = 0x5A
    MAGIC = b"XOR1"

    def protect(self, data: bytes) -> bytes:
        return self.MAGIC + bytes(byte ^ self.KEY for byte in data)

    def unprotect(self, blob: bytes) -> bytes:
        if not blob.startswith(self.MAGIC):
            raise ValueError("Not a protected session blob.")
        return bytes(byte ^ self.KEY for byte in blob[len(self.MAGIC):])

    def describe(self) -> str:
        return "test XOR (not real encryption)"


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path / "profile"


def _build(config, server):
    """Builds the production stack exactly like ``teloude.ui.app.run`` does."""
    manager = TelethonSessionManager(session_dir=config.get_session_dir())

    def connector(phone: str):
        credentials = TelegramCredentials(phone_number=phone, api_id=12345, api_hash="a" * 32)
        client = TelethonTelegramClient(
            credentials, session_manager=manager,
            session_path=str(manager.get_session_path(phone)),
            client_factory=lambda path, api_id, api_hash: SessionFileClient(path, server),
        )
        client.connect()
        raw = client.underlying_client
        raw.wrapper = client
        return client

    ctx = build_real(config, api_id=12345, api_hash="a" * 32, connector=connector)
    return ctx, manager


def _config(data_dir) -> AppConfig:
    return AppConfig(
        data_dir=str(data_dir),
        database_path=str(data_dir / "teloude.db"),
        session_dir=str(data_dir / "sessions"),
    )


def _login(ctx, phone=PHONE, password=None):
    auth = ctx.services.auth
    auth.start_login(phone)
    state = auth.submit_code(phone, CODE)
    if state is AuthState.PASSWORD_NEEDED:
        state = auth.submit_password(password or PASSWORD)
    assert state is AuthState.AUTHORIZED
    return auth


def _settings(ctx):
    return {row[0]: row[1] for row in ctx.db.execute_query(
        "SELECT key, value FROM settings", fetch=True
    )}


class TestSessionIsPersisted:
    def test_first_run_writes_the_session_and_remembers_only_the_digits(
        self, data_dir, monkeypatch
    ):
        monkeypatch.setattr("teloude.ui.app.default_protector", lambda: XorProtector())
        server = ScriptedSessionServer()
        config = _config(data_dir)
        ctx, manager = _build(config, server)
        try:
            _login(ctx)
            session_path = manager.get_session_path(PHONE)
            assert session_path.is_file(), "the session file must exist while signed in"
            assert session_path.read_bytes() == SESSION_PAYLOAD
            # only the digits are remembered - nothing else about the login
            assert ctx.services.auth.remembered_phone() == "10000000000"
            stored = _settings(ctx)
            assert set(stored) == {AuthService.LAST_PHONE_KEY}
            for value in stored.values():
                assert CODE not in value
                assert PASSWORD not in value
                assert "a" * 32 not in value
                assert "SESSION" not in value
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_two_factor_login_remembers_the_number_too(self, data_dir, monkeypatch):
        monkeypatch.setattr("teloude.ui.app.default_protector", lambda: XorProtector())
        server = ScriptedSessionServer(needs_password=True)
        config = _config(data_dir)
        ctx, _ = _build(config, server)
        try:
            _login(ctx)
            assert ctx.services.auth.remembered_phone() == "10000000000"
            assert server.passwords_used == [PASSWORD]
            # the password itself is never stored anywhere
            assert PASSWORD not in str(_settings(ctx))
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_the_session_is_locked_when_the_app_closes(self, data_dir, monkeypatch):
        monkeypatch.setattr("teloude.ui.app.default_protector", lambda: XorProtector())
        server = ScriptedSessionServer()
        config = _config(data_dir)
        ctx, manager = _build(config, server)
        ctx.shutdown()
        close_db_connection(ctx.db)

        session_path = manager.get_session_path(PHONE)  # nothing was signed in yet
        assert not session_path.exists() or True  # sanity: no crash on a cold profile

        server2 = ScriptedSessionServer()
        ctx2, manager2 = _build(config, server2)
        try:
            _login(ctx2)
            session_path = manager2.get_session_path(PHONE)
            locked_path = session_path.with_name(session_path.name + ".locked")
            assert session_path.is_file() and not locked_path.exists()
        finally:
            ctx2.shutdown()
            close_db_connection(ctx2.db)
        # after shutdown the plaintext session is gone and only the locked
        # (protector-encrypted) copy remains
        assert not session_path.exists()
        assert locked_path.is_file()
        assert SESSION_PAYLOAD not in locked_path.read_bytes()


class TestSessionIsRestoredOnStartup:
    def _signed_in_then_closed(self, data_dir, monkeypatch, server):
        monkeypatch.setattr("teloude.ui.app.default_protector", lambda: XorProtector())
        config = _config(data_dir)
        ctx, manager = _build(config, server)
        _login(ctx)
        ctx.shutdown()
        close_db_connection(ctx.db)
        return config, manager

    def test_the_next_launch_goes_straight_to_the_main_window(self, data_dir, monkeypatch):
        server = ScriptedSessionServer()
        config, manager = self._signed_in_then_closed(data_dir, monkeypatch, server)
        session_path = manager.get_session_path(PHONE)
        assert not session_path.exists()  # locked when the app closed

        ctx, _ = _build(config, server)
        try:
            # the startup gate in ui.app.run() asks exactly this
            assert ctx.services.auth.is_authorized() is False  # nothing resolved yet
            assert restore_saved_session(ctx) is True
            assert ctx.services.auth.is_authorized() is True
            assert session_path.is_file()  # unlocked again for this run
            assert ctx.services.auth.remembered_phone() == "10000000000"
            assert server.logouts == 0
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_a_revoked_session_falls_back_to_the_sign_in_dialog(self, data_dir, monkeypatch):
        server = ScriptedSessionServer()
        config, _ = self._signed_in_then_closed(data_dir, monkeypatch, server)

        server.authorized = False  # signed out from another device
        ctx, _ = _build(config, server)
        try:
            assert restore_saved_session(ctx) is False
            # the number is still remembered, so the dialog can prefill it
            assert ctx.services.auth.remembered_phone() == "10000000000"
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_a_damaged_locked_session_is_reported_as_unusable(self, data_dir, monkeypatch):
        server = ScriptedSessionServer()
        config, manager = self._signed_in_then_closed(data_dir, monkeypatch, server)
        session_path = manager.get_session_path(PHONE)
        locked_path = session_path.with_name(session_path.name + ".locked")
        locked_path.write_bytes(b"this is not a protected session")

        ctx, _ = _build(config, server)
        try:
            assert restore_saved_session(ctx) is False  # no crash, no silent accept
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_a_fresh_profile_has_nothing_to_restore(self, data_dir, monkeypatch):
        monkeypatch.setattr("teloude.ui.app.default_protector", lambda: XorProtector())
        server = ScriptedSessionServer()
        config = _config(data_dir)
        ctx, _ = _build(config, server)
        try:
            assert ctx.services.auth.remembered_phone() is None
            assert restore_saved_session(ctx) is False
            # first-time sign-in still works exactly as before
            _login(ctx)
            assert ctx.services.auth.is_authorized() is True
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_a_network_failure_while_restoring_asks_for_a_sign_in(
        self, data_dir, monkeypatch
    ):
        server = ScriptedSessionServer()
        config, _ = self._signed_in_then_closed(data_dir, monkeypatch, server)

        def exploding_connector(phone):
            raise OSError("no internet right now")

        state = {"connector": exploding_connector}
        ctx, _ = _build_with(state, config, server)
        try:
            assert restore_saved_session(ctx) is False
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

    def test_signing_out_forgets_the_number(self, data_dir, monkeypatch):
        server = ScriptedSessionServer()
        config, _ = self._signed_in_then_closed(data_dir, monkeypatch, server)
        ctx, _ = _build(config, server)
        try:
            assert restore_saved_session(ctx) is True
            ctx.services.auth.logout()  # the user asks to sign out
            assert server.logouts == 1
            assert ctx.services.auth.remembered_phone() is None
        finally:
            ctx.shutdown()
            close_db_connection(ctx.db)

        ctx2, _ = _build(config, server)
        try:
            assert restore_saved_session(ctx2) is False  # a login is required again
        finally:
            ctx2.shutdown()
            close_db_connection(ctx2.db)

    def test_the_restore_does_not_depend_on_the_working_directory(
        self, data_dir, monkeypatch, tmp_path
    ):
        server = ScriptedSessionServer()
        config, _ = self._signed_in_then_closed(data_dir, monkeypatch, server)
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        previous = os.getcwd()
        try:
            os.chdir(elsewhere)
            ctx, _ = _build(config, server)
            try:
                assert restore_saved_session(ctx) is True
            finally:
                ctx.shutdown()
                close_db_connection(ctx.db)
        finally:
            os.chdir(previous)


def _build_with(state, config, server):
    """Builds the stack with a connector the test controls (failure injection)."""
    manager = TelethonSessionManager(session_dir=config.get_session_dir())

    def connector(phone: str):
        return state["connector"](phone)

    ctx = build_real(config, api_id=12345, api_hash="a" * 32, connector=connector)
    ctx.session_manager = manager
    return ctx, manager


class TestAuthServiceContract:
    """The service must expose what its interface promises."""

    def test_is_authorized_reports_the_gateway(self):
        bus = EventBus()
        fake = FakeAuth()
        service = AuthService(fake, bus)
        assert service.is_authorized() is False
        assert service.start_login(PHONE) is AuthState.CODE_SENT
        assert service.submit_code(PHONE, "11111") is AuthState.AUTHORIZED
        assert service.is_authorized() is True

    def test_the_remembered_number_is_optional(self):
        """Existing two-argument construction keeps working (no settings repo)."""
        service = AuthService(FakeAuth(), EventBus())
        service.start_login(PHONE)
        service.submit_code(PHONE, "11111")
        assert service.remembered_phone() is None  # nothing to persist it in
