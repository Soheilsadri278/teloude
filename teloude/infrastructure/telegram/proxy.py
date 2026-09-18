# teloude/infrastructure/telegram/proxy.py
"""Proxy configuration for Teloude's single Telegram connection layer.

V1 supports one proxy type: an MTProto proxy, exactly as Telegram offers it
(server, port, secret). This module holds plain configuration data and the
small store that persists it in the existing settings table; it never talks to
Telegram and it never imports Telethon - the transport itself is chosen in
``telethon_client.py``, the only module allowed to see Telethon.

Security, because a proxy secret is a credential:

* ``ProxyConfig.secret`` is excluded from ``repr()``, from ``str()`` and from
  every log line written here; only ``endpoint()``/``describe()`` - host and
  port - are ever logged or shown.
* At rest the secret is written through the existing ``SessionProtector``:
  Windows DPAPI (current user scope) on Windows, and everywhere else the
  explicitly labelled plaintext fallback, described honestly by ``mechanism``.
  Host, port, type and the enabled flag are not secrets and stay readable.
* ``ProxyConfigError`` carries a user-facing sentence and never echoes the
  secret back.
"""
import base64
import binascii
import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional

from teloude.infrastructure.security.dpapi import SessionProtector, default_protector

from .exceptions import ProxyConfigError

logger = logging.getLogger("TelegramProxy")

# Settings keys (the existing settings table, no new storage mechanism).
SETTINGS_PREFIX = "telegram.proxy."
KEY_ENABLED = SETTINGS_PREFIX + "enabled"
KEY_KIND = SETTINGS_PREFIX + "kind"
KEY_HOST = SETTINGS_PREFIX + "host"
KEY_PORT = SETTINGS_PREFIX + "port"
KEY_SECRET = SETTINGS_PREFIX + "secret"  # protected blob, never the raw secret

MIN_PORT = 1
MAX_PORT = 65535
MIN_SECRET_BYTES = 16


class ProxyKind(str, Enum):
    """The proxy types Teloude can use. Telegram's MTProto proxy is the first."""

    MT_PROTO = "mtproto"

    @property
    def label(self) -> str:
        return "MTProto proxy"


@dataclass
class ProxyConfig:
    """One proxy endpoint plus whether the connection layer should use it.

    ``secret`` is a credential: it is kept out of ``repr()`` (and therefore out
    of logs, error reports and object dumps) and only ever reaches Telethon.
    """

    kind: ProxyKind = ProxyKind.MT_PROTO
    host: str = ""
    port: int = 0
    secret: str = field(default="", repr=False)
    enabled: bool = False

    def clean(self) -> "ProxyConfig":
        """Whitespace-trimmed copy. The secret is never lowercased: base64 is
        case-sensitive, and hex is accepted as typed."""
        try:
            port = int(self.port or 0)
        except (TypeError, ValueError):
            port = 0
        return replace(
            self,
            kind=self.kind if isinstance(self.kind, ProxyKind) else ProxyKind(str(self.kind)),
            host=(self.host or "").strip(),
            port=port,
            secret=(self.secret or "").strip(),
            enabled=bool(self.enabled),
        )

    # -- validation ----------------------------------------------------------
    def validate(self) -> None:
        """Raises ProxyConfigError with a user-facing sentence. Never logs."""
        if not self.host:
            raise ProxyConfigError(
                "Enter the proxy server address, for example mtproxy.example.com."
            )
        if not MIN_PORT <= int(self.port or 0) <= MAX_PORT:
            raise ProxyConfigError(f"Enter a port between {MIN_PORT} and {MAX_PORT}.")
        if not self.secret:
            raise ProxyConfigError(
                "Enter the secret Telegram shows for this proxy."
            )
        if not secret_is_usable(self.secret):
            raise ProxyConfigError(self._secret_problem())

    def _secret_problem(self) -> str:
        """Why the secret was refused, in words that say what to paste."""
        text = (self.secret or "").strip()
        if _is_miscased_marker(text):
            return (
                "This secret starts with an upper-case EE or DD. Telegram "
                "writes that marker in lower case, and only then is it read as "
                "part of the format - paste the secret exactly as Telegram "
                "shows it."
            )
        return (
            "That secret is not usable. Paste it exactly as Telegram shows it "
            "for this proxy: the 32 hexadecimal characters (with a leading dd "
            "or ee if the proxy has one) or the base64 form. What was pasted "
            f"has {len(text)} characters."
        )

    def is_complete(self) -> bool:
        """True when the fields could actually carry a connection."""
        return bool(self.host) and MIN_PORT <= int(self.port or 0) <= MAX_PORT and secret_is_usable(self.secret)

    # -- what may be shown / logged ------------------------------------------
    def endpoint(self) -> str:
        """``host:port`` - the part that is not a secret."""
        return f"{self.host}:{self.port}" if self.host else ""

    def describe(self) -> str:
        """Human sentence for logs, tooltips and status lines (no secret)."""
        if not self.host:
            return "No proxy configured."
        return f"{self.kind.label} at {self.endpoint()}"

    def __str__(self) -> str:
        return self.describe()


def _decoded_secret(secret: str) -> Optional[bytes]:
    """Bytes behind a Telegram proxy secret, or None when it cannot be one.

    Telegram hands out the same secret in two shapes - hex (32 characters,
    sometimes prefixed with ``dd``/``ee``) and base64 - and this is
    deliberately the *same algorithm the transport will use*
    (``TcpMTProxy.normalize_secret`` in Telethon), so "usable" means exactly
    "the connection can derive a real key from this text". Being stricter than
    the transport would refuse secrets that work, which is what happened to the
    base64 secret ``EERighJJvXrFGRMCIMjdCQ``: an earlier version of this check
    stripped the leading ``EE`` as if it were a marker, cut the 16-byte key down
    to 15 bytes, and called a perfectly good secret unusable.

    The ``dd``/``ee`` marker is stripped **only in lower case** - the rule
    Telegram and the transport both use. A base64 secret may legitimately begin
    with "EE"-looking characters, so the check stays case-sensitive.
    """
    candidate = (secret or "").strip()
    if not candidate:
        return None
    if candidate[:2] in ("dd", "ee"):  # lower case only: see above
        candidate = candidate[2:]
    try:
        raw = bytes.fromhex(candidate)
    except ValueError:
        padded = candidate + "=" * (-len(candidate) % 4)
        try:
            raw = base64.b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return None
    return raw if len(raw) >= MIN_SECRET_BYTES else None


def _is_miscased_marker(secret: str) -> bool:
    """True for an ``EE``/``DD``-prefixed hex secret, the one shape that cannot work.

    Telegram writes that marker in lower case, and only then does the transport
    treat it as a marker; ``EE0011...`` would be read as a 17-byte secret and
    fail at the proxy with an error that says nothing about the real cause.
    Recognising the shape lets the user be told what to fix.
    """
    marker = secret[:2]
    if len(secret) != 34 or marker.lower() not in ("dd", "ee") or marker == marker.lower():
        return False
    try:
        return len(bytes.fromhex(secret[2:])) >= MIN_SECRET_BYTES
    except ValueError:
        return False


def secret_is_usable(secret: str) -> bool:
    """True when this is a real MTProto proxy secret *this transport can use*.

    Two ways to fail: text that does not decode to at least 16 bytes at all, and
    an ``ee``/``dd`` marker written in upper case - the transport reads that as a
    17-byte secret and would quietly use the wrong key, so it is refused here
    with a message that says what to fix (see ``_is_miscased_marker``).
    """
    return _decoded_secret(secret) is not None and not _is_miscased_marker(secret)


class ProxySettingsStore:
    """Reads/writes the proxy configuration through the settings repository.

    The secret is stored as a protected blob (base64 of the protector's output),
    so the settings table never holds it in the clear.
    """

    def __init__(self, settings: Any, protector: Optional[SessionProtector] = None):
        self._settings = settings
        self._protector = protector or default_protector()

    @property
    def mechanism(self) -> str:
        """Honest description of how the secret is protected at rest."""
        return self._protector.describe()

    def load(self) -> ProxyConfig:
        """Current configuration; unreadable secret yields an empty one.

        A secret that cannot be unprotected (a different Windows user, a
        restored settings table) must never be guessed at: the rest of the
        configuration is returned and the user re-enters the secret.
        """
        kind = ProxyKind.MT_PROTO
        stored_kind = (self._read(KEY_KIND) or "").strip()
        for candidate in ProxyKind:
            if stored_kind == candidate.value:
                kind = candidate
                break
        raw_port = (self._read(KEY_PORT) or "").strip()
        try:
            port = int(raw_port)
        except ValueError:
            port = 0
        return ProxyConfig(
            kind=kind,
            host=(self._read(KEY_HOST) or "").strip(),
            port=port,
            secret=self._unprotect_secret(self._read(KEY_SECRET)),
            enabled=(self._read(KEY_ENABLED) or "").strip().lower() in ("1", "true", "yes"),
        )

    def save(self, config: ProxyConfig) -> ProxyConfig:
        """Persists the configuration and returns the normalized copy."""
        config = config.clean()
        self._settings.set(KEY_ENABLED, "1" if config.enabled else "0")
        self._settings.set(KEY_KIND, config.kind.value)
        self._settings.set(KEY_HOST, config.host)
        self._settings.set(KEY_PORT, str(config.port or 0))
        self._settings.set(KEY_SECRET, self._protect_secret(config.secret))
        # Only the non-secret half is ever logged.
        logger.info(
            "Proxy configuration saved (%s, enabled=%s, secret=<protected>).",
            config.endpoint() or "no server", config.enabled,
        )
        return config

    def clear(self) -> None:
        """Forgets the proxy entirely (used by tests and by a reset action)."""
        for key in (KEY_ENABLED, KEY_KIND, KEY_HOST, KEY_PORT, KEY_SECRET):
            delete = getattr(self._settings, "delete", None)
            if callable(delete):
                delete(key)
            else:  # a get/set-only facade: an empty value means "unset"
                self._settings.set(key, "")
        logger.info("Proxy configuration cleared.")

    # -- internals -----------------------------------------------------------
    def _read(self, key: str) -> str:
        try:
            return self._settings.get(key, "") or ""
        except Exception as exc:  # a broken settings row must not stop startup
            logger.warning("Could not read %s: %s", key, type(exc).__name__)
            return ""

    def _protect_secret(self, secret: str) -> str:
        if not secret:
            return ""
        try:
            blob = self._protector.protect(secret.encode("utf-8"))
        except Exception as exc:
            # Never fall back to storing it in the clear.
            raise ProxyConfigError(
                "The proxy secret could not be protected on this system, so it "
                "was not saved."
            ) from exc
        return base64.b64encode(blob).decode("ascii")

    def _unprotect_secret(self, stored: Optional[str]) -> str:
        if not stored:
            return ""
        try:
            blob = base64.b64decode(stored.encode("ascii"))
            return self._protector.unprotect(blob).decode("utf-8")
        except Exception as exc:
            logger.warning(
                "Stored proxy secret could not be read (%s); enter it again.",
                type(exc).__name__,
            )
            return ""
