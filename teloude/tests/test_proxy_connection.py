# teloude/tests/test_proxy_connection.py
"""The proxy/connection layer: configuration, storage, state, one-client rule.

No network and no Qt here: the layer is driven through an injected raw client
factory, exactly the way the application drives it through Telethon.
"""
import logging
import time

import pytest

from teloude.application.services import ServiceError
from teloude.config import AppConfig
from teloude.infrastructure.security.dpapi import SessionProtector
from teloude.infrastructure.telegram.connection import (
    ConnectionState,
    TelegramConnection,
    redact,
)
from teloude.infrastructure.telegram.connection_state import ConnectionStatus
from teloude.infrastructure.telegram.exceptions import ConnectionStateError, ProxyConfigError
from teloude.infrastructure.telegram.fakes import FakeConnectionClient, fake_client_factory
from teloude.infrastructure.telegram.proxy import (
    KEY_ENABLED,
    KEY_HOST,
    KEY_PORT,
    KEY_SECRET,
    ProxyConfig,
    ProxyKind,
    ProxySettingsStore,
    secret_is_usable,
)
from teloude.ui.app import build_real

HEX_SECRET = "00112233445566778899aabbccddeeff"      # 16 bytes, as Telegram prints it
DD_SECRET = "dd" + HEX_SECRET                        # padded-random transport
BASE64_SECRET = "ABEiM0RVZneImaq7zN3u/w=="           # the same 16 bytes, base64


class MemorySettings:
    """The settings repository as the store sees it (get/set/delete by key)."""

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class ReversingProtector(SessionProtector):
    """A protector whose output is recognisably not the input (no DPAPI needed)."""

    MARKER = b"PROTECTED:"

    def protect(self, data: bytes) -> bytes:
        return self.MARKER + data[::-1]

    def unprotect(self, blob: bytes) -> bytes:
        if not blob.startswith(self.MARKER):
            raise ValueError("not a protected blob")
        return blob[len(self.MARKER):][::-1]

    def describe(self) -> str:
        return "test protector"


@pytest.fixture()
def settings():
    return MemorySettings()


@pytest.fixture()
def store(settings):
    return ProxySettingsStore(settings, protector=ReversingProtector())


class RecordingFactory:
    """Raw client factory that records how it was called."""

    def __init__(self, client=None):
        self.calls = []
        self.clients = []
        self._client = client

    def __call__(self, session_path, api_id, api_hash, **kwargs):
        self.calls.append({"session_path": session_path, "api_id": api_id,
                           "api_hash": api_hash, "kwargs": kwargs})
        if self._client is None:
            client = FakeConnectionClient()
        elif callable(self._client):
            client = self._client()
        else:
            client = self._client
        client.proxy_seen = kwargs.get("proxy")
        self.clients.append(client)
        return client


def make_connection(tmp_path, settings=None, factory=None, **kwargs):
    store = ProxySettingsStore(settings or MemorySettings(), protector=ReversingProtector())
    connection = TelegramConnection(
        api_id=12345, api_hash="a" * 32, session_dir=str(tmp_path / "sessions"),
        proxy_store=store, client_factory=factory or fake_client_factory, **kwargs
    )
    return connection


PROXY = ProxyConfig(host="mtproxy.example.com", port=443, secret=HEX_SECRET, enabled=True)


# ------------------------------------------------------------------ config ---
class TestProxyConfig:
    @pytest.mark.parametrize("secret", [HEX_SECRET, DD_SECRET, BASE64_SECRET,
                                        HEX_SECRET.upper()])
    def test_every_secret_shape_telegram_hands_out_is_accepted(self, secret):
        assert secret_is_usable(secret)
        ProxyConfig(host="p.example.com", port=443, secret=secret).validate()

    @pytest.mark.parametrize("secret", ["", "1234", "not a secret!", "z" * 16, "1234abcd"])
    def test_a_broken_secret_is_rejected(self, secret):
        assert not secret_is_usable(secret)
        with pytest.raises(ProxyConfigError):
            ProxyConfig(host="p.example.com", port=443, secret=secret).validate()

    def test_missing_fields_are_named_for_the_user(self):
        with pytest.raises(ProxyConfigError, match="server address"):
            ProxyConfig(port=443, secret=HEX_SECRET).validate()
        with pytest.raises(ProxyConfigError, match="port"):
            ProxyConfig(host="p.example.com", port=0, secret=HEX_SECRET).validate()
        with pytest.raises(ProxyConfigError, match="between 1 and 65535"):
            ProxyConfig(host="p.example.com", port=70000, secret=HEX_SECRET).validate()
        with pytest.raises(ProxyConfigError, match="secret"):
            ProxyConfig(host="p.example.com", port=443).validate()

    def test_validation_never_puts_the_secret_in_the_message(self):
        with pytest.raises(ProxyConfigError) as error:
            ProxyConfig(host="p.example.com", port=443, secret="hunter2hunter2!!").validate()
        assert "hunter2" not in str(error.value)

    def test_cleaning_trims_without_touching_the_secret_case(self):
        messy = ProxyConfig(host="  p.example.com ", port=443,
                            secret="  " + BASE64_SECRET + "\n", enabled=1)
        clean = messy.clean()
        assert (clean.host, clean.secret, clean.enabled) == ("p.example.com", BASE64_SECRET, True)

    def test_the_secret_never_appears_in_repr_or_text(self):
        config = ProxyConfig(host="p.example.com", port=443, secret=HEX_SECRET, enabled=True)
        for text in (repr(config), str(config), config.describe(), config.endpoint()):
            assert HEX_SECRET not in text
        assert "p.example.com:443" in config.describe()

    def test_a_disabled_configuration_may_still_be_incomplete(self):
        assert ProxyConfig().enabled is False  # what the store writes before a first save
        assert ProxyConfig().is_complete() is False


# ----------------------------------------------------------------- storage ---
class TestProxySettingsStore:
    def test_round_trip_keeps_every_field(self, store):
        store.save(PROXY)
        loaded = store.load()
        assert (loaded.kind, loaded.host, loaded.port, loaded.enabled) == (
            ProxyKind.MT_PROTO, "mtproxy.example.com", 443, True
        )
        assert loaded.secret == HEX_SECRET

    def test_the_secret_is_never_written_in_the_clear(self, store, settings):
        store.save(PROXY)
        assert HEX_SECRET not in settings.data[KEY_SECRET]
        assert settings.data[KEY_SECRET]  # something was stored, protected
        # The non-secret half stays readable, so a user can see what is set.
        assert settings.data[KEY_HOST] == "mtproxy.example.com"
        assert settings.data[KEY_PORT] == "443"
        assert settings.data[KEY_ENABLED] == "1"

    def test_the_secret_goes_through_the_protector(self, store, settings):
        store.save(PROXY)
        import base64

        raw = base64.b64decode(settings.data[KEY_SECRET])
        assert raw.startswith(ReversingProtector.MARKER)

    def test_the_mechanism_is_reported_honestly(self, store):
        assert store.mechanism == "test protector"

    def test_a_secret_that_cannot_be_read_is_dropped_not_guessed(self, store, settings):
        store.save(PROXY)
        settings.data[KEY_SECRET] = "bm90LXByb3RlY3RlZA=="  # base64 of "not-protected"
        loaded = store.load()
        assert loaded.secret == ""
        assert loaded.host == "mtproxy.example.com"  # the rest survives

    def test_saving_an_empty_secret_clears_it(self, store, settings):
        store.save(PROXY)
        store.save(ProxyConfig(host="mtproxy.example.com", port=443, enabled=False))
        assert settings.data[KEY_SECRET] == ""
        assert store.load().secret == ""

    def test_clearing_forgets_everything(self, store):
        store.save(PROXY)
        store.clear()
        assert store.load() == ProxyConfig()


# ---------------------------------------------------------------- the layer ---
class TestConnectionLayer:
    def test_nothing_changes_when_no_proxy_is_configured(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        connection.connect("+15005550006")
        call = factory.calls[0]
        assert call["kwargs"] == {}, "a client without a proxy must be built as before"
        assert call["session_path"].endswith(".session")
        assert connection.state is ConnectionState.CONNECTED

    def test_an_enabled_proxy_reaches_the_client(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        connection.apply_proxy(PROXY)
        connection.connect("+15005550006")
        assert factory.calls[0]["kwargs"]["proxy"].endpoint() == "mtproxy.example.com:443"
        assert factory.clients[0].proxy_seen.secret == HEX_SECRET

    def test_an_enabled_but_incomplete_proxy_connects_directly(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        with pytest.raises(ProxyConfigError):
            connection.apply_proxy(ProxyConfig(host="mtproxy.example.com", enabled=True))
        # A disabled (or half-typed) configuration never breaks the connection.
        connection.apply_proxy(ProxyConfig(host="mtproxy.example.com", enabled=False))
        connection.connect("+15005550006")
        assert factory.calls[0]["kwargs"] == {}

    def test_applying_a_configuration_persists_it(self, tmp_path, settings):
        connection = make_connection(tmp_path, settings=settings)
        connection.apply_proxy(PROXY)
        saved = ProxySettingsStore(settings, protector=ReversingProtector()).load()
        assert saved.enabled and saved.host == "mtproxy.example.com"
        assert saved.secret == HEX_SECRET

    def test_enable_and_disable_keep_the_endpoint(self, tmp_path):
        connection = make_connection(tmp_path)
        connection.apply_proxy(PROXY)
        connection.set_proxy_enabled(False)
        config = connection.proxy_config()
        assert config.enabled is False
        assert (config.host, config.port, config.secret) == (
            "mtproxy.example.com", 443, HEX_SECRET
        )
        assert connection.active_proxy() is None

    def test_state_reports_the_four_indicator_states(self):
        for status, expected in (
            (ConnectionStatus.DISCONNECTED, ConnectionState.DISCONNECTED),
            (ConnectionStatus.CONNECTING, ConnectionState.CONNECTING),
            (ConnectionStatus.AUTHENTICATING, ConnectionState.CONNECTING),
            (ConnectionStatus.READY, ConnectionState.CONNECTED),
            (ConnectionStatus.ERROR, ConnectionState.ERROR),
        ):
            assert ConnectionState.from_status(status) is expected

    def test_a_failing_connect_ends_in_the_error_state(self, tmp_path):
        factory = RecordingFactory(FakeConnectionClient(fail=True))
        connection = make_connection(tmp_path, factory=factory)
        with pytest.raises(ConnectionStateError):
            connection.connect("+15005550006")
        assert connection.state is ConnectionState.ERROR
        assert "Could not reach Telegram" in connection.detail
        assert connection.detail == connection.snapshot().detail

    def test_status_changes_are_published_on_the_bus(self, tmp_path):
        from teloude.application.services import EventBus

        bus = EventBus()
        seen = []
        bus.subscribe("connection_state", seen.append)
        connection = make_connection(tmp_path)
        connection.attach_bus(bus)
        connection.connect("+15005550006")
        states = [payload["state"] for payload in seen]
        assert ConnectionState.CONNECTING.value in states
        assert states[-1] == ConnectionState.CONNECTED.value
        assert all(HEX_SECRET not in str(payload) for payload in seen)

    def test_listeners_see_a_snapshot_without_the_secret(self, tmp_path):
        connection = make_connection(tmp_path)
        connection.apply_proxy(PROXY)
        seen = []
        connection.add_listener(seen.append)
        connection.connect("+15005550006")
        assert seen and seen[-1].state is ConnectionState.CONNECTED
        assert HEX_SECRET not in str([snapshot.as_payload() for snapshot in seen])

    def test_reconnecting_swaps_the_live_client(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        connection.connect("+15005550006")
        connection.apply_proxy(PROXY)
        connection.reconnect()
        assert len(factory.calls) == 2
        assert "proxy" not in factory.calls[0]["kwargs"]
        assert factory.calls[1]["kwargs"]["proxy"].endpoint() == "mtproxy.example.com:443"
        assert connection.state is ConnectionState.CONNECTED

    def test_reconnect_hooks_get_the_new_client(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        rebound = []
        connection.add_reconnect_hook(rebound.append)
        connection.connect("+15005550006")
        assert rebound == [], "the first connection is wired by the caller, not a rebind"
        connection.apply_proxy(PROXY)
        connection.reconnect()
        assert len(rebound) == 1
        # The hook receives the client wrapper, the thing the app wires gateways
        # from - and it is the one this connection just built.
        assert rebound[0].underlying_client is factory.clients[1]

    def test_reconnecting_without_a_session_says_so(self, tmp_path):
        connection = make_connection(tmp_path)
        with pytest.raises(ProxyConfigError, match="Sign in first"):
            connection.reconnect()

    def test_disconnect_returns_to_the_disconnected_state(self, tmp_path):
        connection = make_connection(tmp_path)
        connection.connect("+15005550006")
        connection.disconnect()
        assert connection.state is ConnectionState.DISCONNECTED


class TestConnectionTest:
    def test_a_working_configuration_is_reported_as_connected(self, tmp_path):
        connection = make_connection(tmp_path)
        result = connection.test_proxy(PROXY)
        assert result.ok and result.state is ConnectionState.CONNECTED
        assert result.message == "Connected through mtproxy.example.com:443."
        assert connection.state is ConnectionState.CONNECTED

    def test_a_direct_test_uses_no_proxy(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        result = connection.test_proxy(ProxyConfig(enabled=False))
        assert result.ok and "directly" in result.message
        assert factory.calls[0]["kwargs"] == {}

    def test_an_unreachable_proxy_is_reported_as_an_error(self, tmp_path):
        factory = RecordingFactory(FakeConnectionClient(fail=True))
        connection = make_connection(tmp_path, factory=factory)
        result = connection.test_proxy(PROXY)
        assert not result.ok and result.state is ConnectionState.ERROR
        assert connection.state is ConnectionState.ERROR
        assert "Could not reach Telegram" in result.message
        assert result.proxy == "mtproxy.example.com:443"

    def test_a_silent_proxy_times_out_instead_of_hanging(self, tmp_path):
        factory = RecordingFactory(FakeConnectionClient(delay=1.5))
        connection = make_connection(tmp_path, factory=factory, probe_timeout=0.1)
        started = time.monotonic()
        result = connection.test_proxy(PROXY)
        assert not result.ok and result.state is ConnectionState.ERROR
        assert "No answer" in result.message
        assert time.monotonic() - started < 1.0, "the page must not freeze on a dead proxy"

    def test_an_invalid_configuration_is_rejected_before_any_network_use(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        result = connection.test_proxy(ProxyConfig(host="p.example.com", port=443, enabled=True))
        assert not result.ok and factory.calls == []

    def test_a_secret_in_an_error_message_is_redacted(self, tmp_path):
        class LeakyClient(FakeConnectionClient):
            def __init__(self, secret):
                super().__init__()
                self.secret = secret

            def connect(self):
                raise ConnectionStateError(f"cannot reach the proxy with secret {self.secret}")

        def leaky_factory(session_path, api_id, api_hash, proxy=None, **kwargs):
            return LeakyClient(proxy.secret) if proxy is not None else LeakyClient("")

        connection = make_connection(tmp_path, factory=leaky_factory)
        result = connection.test_proxy(PROXY)
        assert not result.ok
        assert HEX_SECRET not in result.message
        assert "<secret>" in result.message

    def test_redact_rewrites_every_case_of_the_secret(self):
        text = redact(f"{HEX_SECRET} / {HEX_SECRET.upper()} / other", HEX_SECRET)
        assert HEX_SECRET not in text and HEX_SECRET.upper() not in text
        assert text.count("<secret>") == 2
        assert redact("short", "ab") == "short", "tiny values are left alone"

    def test_the_probe_never_touches_the_real_session(self, tmp_path):
        connection = make_connection(tmp_path)
        sessions = tmp_path / "sessions"
        connection.test_proxy(PROXY)
        assert not sessions.exists(), "the probe session lives in its own temp directory"

    def test_a_test_does_not_disturb_a_live_connection(self, tmp_path):
        factory = RecordingFactory()
        connection = make_connection(tmp_path, factory=factory)
        connection.connect("+15005550006")
        assert connection.state is ConnectionState.CONNECTED
        factory._client = FakeConnectionClient(fail=True)  # the probe now fails
        result = connection.test_proxy(PROXY)
        assert not result.ok
        assert connection.state is ConnectionState.CONNECTED, "the live session keeps its state"
        assert connection.detail.startswith("Connected to Telegram")


# ------------------------------------- one configuration for all communication ---
class RecordingRawClient:
    """A scripted raw Telethon client that records what the app asks of it."""

    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append(("connect", None))
        return True

    def send_code_request(self, phone):
        from types import SimpleNamespace

        self.calls.append(("send_code_request", phone))
        return SimpleNamespace(phone_code_hash="hash:1")

    def is_user_authorized(self):
        self.calls.append(("is_user_authorized", None))
        return True

    async def get_dialogs(self):
        self.calls.append(("get_dialogs", None))
        return []

    def disconnect(self):
        self.calls.append(("disconnect", None))

    def __call__(self, request):
        self.calls.append(("request", type(request).__name__))
        return None


class TestOneConnectionForEverything:
    """The proxy is configured once and reaches every feature.

    Authentication, session creation, upload, download, sync and search all run
    through the client the connection layer builds: there is no second client
    and no feature-local proxy handling to drift out of step.
    """

    def _stack(self, tmp_path):
        factory = RecordingFactory(RecordingRawClient)
        connection = TelegramConnection(
            api_id=12345, api_hash="a" * 32, session_dir=str(tmp_path / "sessions"),
            client_factory=factory,
        )
        config = AppConfig(
            data_dir=str(tmp_path / "data"), database_path=str(tmp_path / "data" / "t.db"),
            session_dir=str(tmp_path / "sessions"),
        )
        ctx = build_real(config, 12345, "a" * 32,
                         connector=connection.connect, connection=connection)
        return factory, connection, ctx

    def test_authentication_and_the_gateways_share_one_proxied_client(self, tmp_path):
        factory, connection, ctx = self._stack(tmp_path)
        try:
            connection.apply_proxy(PROXY)
            ctx.services.auth.start_login("+15005550006")   # builds the client
            assert len(factory.calls) == 1, "one client, not one per feature"
            assert factory.calls[0]["kwargs"]["proxy"].endpoint() == "mtproxy.example.com:443"
            raw = factory.clients[0]

            # ... and the storage gateway is on that same client: adopting a
            # storage asks Telegram for the chat list through it (the scripted
            # server has none, which is why this raises).
            with pytest.raises(ServiceError):
                ctx.services.storages.adopt_storage("Archive")
            assert ("get_dialogs", None) in raw.calls
            assert connection.state is ConnectionState.CONNECTED
        finally:
            ctx.shutdown()

    def test_switching_the_proxy_reaches_the_live_features(self, tmp_path):
        factory, connection, ctx = self._stack(tmp_path)
        try:
            ctx.services.auth.start_login("+15005550006")
            first = factory.clients[0]
            assert ("get_dialogs", None) not in first.calls

            connection.apply_proxy(PROXY)
            connection.reconnect("+15005550006")

            assert len(factory.calls) == 2
            second = factory.clients[1]
            assert second is not first
            assert factory.calls[1]["kwargs"]["proxy"].secret == HEX_SECRET
            # The gateways now talk to the new client, without a restart.
            with pytest.raises(ServiceError):
                ctx.services.storages.adopt_storage("Archive")
            assert ("get_dialogs", None) in second.calls
        finally:
            ctx.shutdown()

    def test_without_a_proxy_nothing_about_the_stack_changes(self, tmp_path):
        factory, connection, ctx = self._stack(tmp_path)
        try:
            ctx.services.auth.start_login("+15005550006")
            assert factory.calls[0]["kwargs"] == {}
            assert connection.proxy_config() == ProxyConfig()
        finally:
            ctx.shutdown()


def test_nothing_in_the_proxy_path_logs_the_secret(tmp_path, caplog, settings):
    connection = make_connection(tmp_path, settings=settings)
    with caplog.at_level(logging.DEBUG):
        connection.apply_proxy(PROXY)
        connection.connect("+15005550006")
        connection.disconnect()
    assert HEX_SECRET not in caplog.text
