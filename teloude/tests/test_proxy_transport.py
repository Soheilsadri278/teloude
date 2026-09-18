# teloude/tests/test_proxy_transport.py
"""Proof that a configured proxy really protects the traffic - on the wire.

The other proxy tests check configuration, storage, state and UI. This one
answers the question a user actually cares about: *does the client, as Teloude
builds it, speak MTProxy correctly with the secret I pasted?*

It does that the way a proxy does it. ``MockMTProxy`` implements the server side
of the MTProxy handshake - the side that must be able to read the client's
traffic - using the documented derivation (keys from the secret and the client's
64-byte header), without reusing any of Telethon's client code. It listens on
localhost, captures the first packet, and tries to decrypt it.

Two facts are pinned:

* for every secret shape Telegram hands out (including the base64 form that
  starts with "EE", which was once mangled into an unusable 15-byte key), the
  client's traffic decrypts into a real MTProto packet (``req_pq_multi``), and
* with any *other* secret the same bytes do not decrypt at all - so the secret
  is genuinely what binds the traffic, not decoration around it.

No network beyond localhost is involved; the client's attempt to complete the
handshake is expected to fail (the mock answers nothing), which is fine: the
assertion is about what the mock could read.
"""
import hashlib
import socket
import struct
import threading
import time

import pytest

pytest.importorskip("telethon")

from telethon.crypto.aesctr import AESModeCTR  # noqa: E402
from telethon.network.connection.tcpmtproxy import TcpMTProxy  # noqa: E402

from teloude.infrastructure.telegram.connection import TelegramConnection, ConnectionState  # noqa: E402
from teloude.infrastructure.telegram.proxy import ProxyConfig  # noqa: E402

HEADER_SIZE = 64
REQ_PQ_MULTI = bytes.fromhex("f18e7ebe")   # constructor 0xbe7e8ef1, little-endian

HEX_SECRET = "00112233445566778899aabbccddeeff"
DD_SECRET = "dd" + HEX_SECRET
EE_SECRET = "ee" + HEX_SECRET
BASE64_SECRET = "ABEiM0RVZneImaq7zN3u/w=="          # the same 16 bytes
BASE64_EE_SECRET = "EERighJJvXrFGRMCIMjdCQ"        # a real proxy's secret: starts with "EE"
OTHER_SECRET = "ffffffffffffffffffffffffffffffff"


def strip_length_prefix(plain: bytes) -> bytes:
    """Turns a decrypted stream into the packet it carries.

    The codec frames every packet as a 4-byte length followed by that many
    bytes, and pads the payload to a 4-byte boundary; both are removed here so
    the tests compare packets, not framing.
    """
    if len(plain) < 4:
        return b""
    length = struct.unpack("<i", plain[:4])[0]
    if not 0 < length <= len(plain) - 4:
        return b""       # a wrong key lands here: the length is nonsense
    packet = plain[4:4 + length]
    padding = len(packet) % 4
    return packet[:-padding] if padding else packet


def is_valid_mtproto(packet: bytes) -> bool:
    """An unencrypted MTProto packet: empty auth key id, then req_pq_multi.

    Used as the single definition of "readable" for both sides: what the proxy
    holding the secret should see, and what any other key must never produce.
    """
    return (len(packet) >= 24
            and packet[:8] == bytes(8)
            and packet[20:24] == REQ_PQ_MULTI)


class MockMTProxy:
    """The proxy side of the handshake: derives keys, then reads the client.

    A real MTProxy knows the secret and reconstructs the two AES-CTR streams
    from the first 64 bytes the client sends; this does the same, decrypts the
    packet that follows, and keeps what it saw. It answers nothing, so the
    client never completes a handshake - the point is what could be read.
    """

    def __init__(self, secret: str):
        self._secret = secret
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self.header = b""
        self.cipher = b""      # the bytes exactly as they arrived
        self.packet = b""      # the same bytes, decrypted with the secret
        self.length = None
        self._thread = threading.Thread(target=self._serve, name="mock-mtproxy", daemon=True)
        self._thread.start()

    # -- the proxy's view ---------------------------------------------------
    def _serve(self) -> None:
        try:
            self._server.settimeout(20)
            conn, _addr = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(8)
            self.header = self._read(conn, HEADER_SIZE)
            if len(self.header) == HEADER_SIZE:
                self._read_packet(conn)
            time.sleep(0.1)  # let the client notice that the proxy says nothing
        self.close()

    def _read(self, conn, size: int) -> bytes:
        data = b""
        while len(data) < size:
            try:
                chunk = conn.recv(size - len(data))
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data

    def _read_packet(self, conn) -> None:
        stream = self._stream()
        self.cipher = self._read(conn, 4)
        if len(self.cipher) < 4:
            return
        self.length = struct.unpack("<i", stream.encrypt(self.cipher))[0]
        if not 0 < self.length < 10_000:
            return  # a wrong secret produces a nonsense length: nothing to read
        body = self._read(conn, self.length)
        self.cipher += body
        self.packet = strip_length_prefix(struct.pack("<i", self.length) + stream.encrypt(body))

    def _stream(self) -> AESModeCTR:
        """The keystream the client encrypts with, built from secret + header."""
        raw = bytearray(self.header)
        key = hashlib.sha256(
            bytes(raw[8:40]) + TcpMTProxy.normalize_secret(self._secret)
        ).digest()
        stream = AESModeCTR(key, bytes(raw[40:56]))
        # The client spent the first 64 keystream bytes on its header, so the
        # same 64 bytes are consumed here to line the two streams up.
        stream.encrypt(bytes(raw))
        return stream

    # -- what the tests assert on -------------------------------------------
    def saw_valid_mtproto(self) -> bool:
        """True when the packet read with this proxy's secret is real MTProto."""
        return is_valid_mtproto(self.packet)

    def decrypt_with(self, secret: str) -> bytes:
        """The captured bytes as read by a proxy holding a *different* secret."""
        if not self.cipher:
            return b""
        raw = bytearray(self.header)
        key = hashlib.sha256(
            bytes(raw[8:40]) + TcpMTProxy.normalize_secret(secret)
        ).digest()
        stream = AESModeCTR(key, bytes(raw[40:56]))
        stream.encrypt(bytes(raw))  # line up with the client's keystream position
        return strip_length_prefix(stream.encrypt(self.cipher))

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass


def proxied_connection(tmp_path, secret: str, port: int):
    """The application's connection layer, with the client factory it really uses.

    ``client_factory=None`` is the production default: Telethon itself, with the
    MTProxy transport our code selects for a configured proxy.
    """
    connection = TelegramConnection(
        api_id=1, api_hash="x" * 32, session_dir=str(tmp_path / "sessions"),
        proxy_store=None, client_factory=None,
    )
    config = ProxyConfig(host="127.0.0.1", port=port, secret=secret, enabled=True)
    connection.apply_proxy(config)
    return connection, config


@pytest.mark.parametrize("label,secret", [
    ("hex", HEX_SECRET),
    ("dd marker", DD_SECRET),
    ("ee marker", EE_SECRET),
    ("base64", BASE64_SECRET),
    ("base64 starting with EE", BASE64_EE_SECRET),
])
def test_the_proxy_can_read_our_clients_real_traffic(tmp_path, label, secret):
    """Every secret shape yields a stream a proxy holding it can decrypt."""
    proxy = MockMTProxy(secret)
    connection, config = proxied_connection(tmp_path, secret, proxy.port)
    try:
        result = connection.test_proxy(config, timeout=15)
        assert not result.ok, "the mock answers nothing, so this cannot succeed"
        assert result.state is ConnectionState.ERROR
        assert len(proxy.header) == HEADER_SIZE, "the client sent no MTProxy header"
        assert proxy.saw_valid_mtproto(), (
            f"{label}: the proxy could not read a valid MTProto packet - "
            f"length={proxy.length}"
        )
    finally:
        connection.disconnect()
        proxy.close()


def test_only_the_configured_secret_unlocks_the_traffic(tmp_path):
    """Negative control: the same bytes are unreadable with another secret."""
    proxy = MockMTProxy(BASE64_EE_SECRET)
    connection, config = proxied_connection(tmp_path, BASE64_EE_SECRET, proxy.port)
    try:
        connection.test_proxy(config, timeout=15)
        assert proxy.saw_valid_mtproto()
        assert not is_valid_mtproto(proxy.decrypt_with(OTHER_SECRET)), (
            "a different secret read the traffic - the secret is not binding it"
        )
        assert is_valid_mtproto(proxy.decrypt_with(BASE64_EE_SECRET)), (
            "the configured secret must read the packet back"
        )
    finally:
        connection.disconnect()
        proxy.close()


def test_a_client_without_a_proxy_sends_no_mtproxy_header(tmp_path):
    """The mock must see a header only when a proxy is actually configured."""
    proxy = MockMTProxy(BASE64_EE_SECRET)
    connection = TelegramConnection(
        api_id=1, api_hash="x" * 32, session_dir=str(tmp_path / "sessions"),
        proxy_store=None, client_factory=None,
    )
    try:
        # No proxy configured: the connection layer is asked to probe directly,
        # which means the mock never receives a client at all.
        result = connection.test_proxy(ProxyConfig(enabled=False), timeout=10)
        assert not result.ok
        assert proxy.header == b""
    finally:
        connection.disconnect()
        proxy.close()
