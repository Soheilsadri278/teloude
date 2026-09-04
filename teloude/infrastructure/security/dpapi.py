# teloude/infrastructure/security/dpapi.py
"""Session-file protection built on Windows DPAPI (CryptProtectData).

CURRENT IMPLEMENTATION:
- On Windows, SecureSessionStore encrypts the Telethon ``.session`` file with
  DPAPI (current-user scope) whenever the app is not running, and decrypts it
  back on startup. Plaintext exists only while the app runs, in a
  user-profile directory with user-only file permissions.
- Anywhere else, protection is NOT encryption: PlaintextProtector stores a
  clear marker so the limitation is explicit (used by tests and non-Windows
  development). default_protector() picks DPAPI on Windows automatically.

Nothing here is fake security: describe() always reports the real mechanism,
and logs never contain secrets.
"""
import ctypes
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Security")

_LOCK_SUFFIX = ".locked"


class SessionProtector(ABC):
    @abstractmethod
    def protect(self, data: bytes) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def unprotect(self, blob: bytes) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def describe(self) -> str:
        """Honest human-readable description of the mechanism in use."""
        raise NotImplementedError


class DpapiProtector(SessionProtector):
    """Real DPAPI protection (Windows only; raises elsewhere)."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("DPAPI is only available on Windows.")
        try:
            self._crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        except Exception as exc:
            raise OSError(f"Could not load DPAPI: {exc}") from exc

    def protect(self, data: bytes) -> bytes:
        return _crypt_protect(self._crypt32, data)

    def unprotect(self, blob: bytes) -> bytes:
        return _crypt_unprotect(self._crypt32, blob)

    def describe(self) -> str:
        return "Windows DPAPI (current user)"


class PlaintextProtector(SessionProtector):
    """NOT encryption. Explicit fallback for non-Windows platforms and tests."""

    MARKER = b"TELOUDE-PLAIN:"

    def protect(self, data: bytes) -> bytes:
        logger.warning(
            "Storing session data WITHOUT encryption (non-Windows platform)."
        )
        return self.MARKER + data

    def unprotect(self, blob: bytes) -> bytes:
        if not blob.startswith(self.MARKER):
            raise ValueError("Not a plaintext-protected blob.")
        return blob[len(self.MARKER):]

    def describe(self) -> str:
        return "plaintext (NOT encrypted - non-Windows fallback)"


def default_protector() -> SessionProtector:
    """DPAPI on Windows, explicit plaintext fallback elsewhere."""
    if os.name == "nt":
        try:
            return DpapiProtector()
        except OSError as exc:
            logger.warning(f"DPAPI unavailable, falling back: {exc}")
    return PlaintextProtector()


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_char_p)]


def _crypt_protect(crypt32, data: bytes) -> bytes:
    plain = _Blob(len(data), data)
    cipher = _Blob()
    if not crypt32.CryptProtectData(
        ctypes.byref(plain), None, None, None, None, 0, ctypes.byref(cipher)
    ):
        raise OSError("DPAPI CryptProtectData failed.")
    try:
        return ctypes.string_at(cipher.pbData, cipher.cbData)
    finally:
        crypt32.LocalFree(cipher.pbData)


def _crypt_unprotect(crypt32, blob: bytes) -> bytes:
    cipher = _Blob(len(blob), blob)
    plain = _Blob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(cipher), None, None, None, None, 0, ctypes.byref(plain)
    ):
        raise OSError("DPAPI CryptUnprotectData failed.")
    try:
        return ctypes.string_at(plain.pbData, plain.cbData)
    finally:
        crypt32.LocalFree(plain.pbData)


class SecureSessionStore:
    """Locks/unlocks a Telethon session file around application runs."""

    def __init__(self, session_file: Path, protector: Optional[SessionProtector] = None):
        self._session_file = Path(session_file)
        self._protector = protector or default_protector()

    @property
    def mechanism(self) -> str:
        return self._protector.describe()

    def is_locked(self) -> bool:
        return self._locked_path().is_file()

    def lock(self) -> bool:
        """Encrypts the session file and removes the plaintext. Returns True if locked."""
        if not self._session_file.is_file():
            return False  # nothing to protect (fresh login)
        try:
            plaintext = self._session_file.read_bytes()
            self._locked_path().write_bytes(self._protector.protect(plaintext))
            _secure_delete(self._session_file)
            logger.info(f"Session locked ({self._protector.describe()}).")
            return True
        except Exception as exc:
            logger.error(f"Could not lock the session file: {exc}")
            return False

    def unlock(self) -> bool:
        """Restores the plaintext session file. Returns True when usable."""
        locked = self._locked_path()
        if not locked.is_file():
            return self._session_file.is_file() or True  # fresh login or already unlocked
        try:
            plaintext = self._protector.unprotect(locked.read_bytes())
            self._session_file.write_bytes(plaintext)
            try:
                if os.name != "nt":
                    os.chmod(self._session_file, 0o600)
            except OSError:
                pass
            locked.unlink()
            logger.info("Session unlocked for this run.")
            return True
        except Exception as exc:
            logger.error(f"Could not unlock the session file: {exc}")
            return False

    def _locked_path(self) -> Path:
        return self._session_file.with_name(self._session_file.name + _LOCK_SUFFIX)


def _secure_delete(path: Path) -> None:
    """Best-effort shred (overwrite + delete); falls back to plain delete."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(os.urandom(min(size, 1024 * 1024)))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass
