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
    """Real DPAPI protection (Windows only; raises elsewhere).

    ``crypt32`` / ``local_free`` are injectable so the marshalling can be tested
    without Windows; production always resolves the real system libraries.
    """

    def __init__(self, crypt32=None, local_free=None):
        if crypt32 is None:
            crypt32 = _load_crypt32()
        if local_free is None:
            local_free = _load_local_free()
        self._crypt32 = crypt32
        self._local_free = local_free

    def protect(self, data: bytes) -> bytes:
        return _crypt_protect(self._crypt32, data, self._local_free)

    def unprotect(self, blob: bytes) -> bytes:
        return _crypt_unprotect(self._crypt32, blob, self._local_free)

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
    """DATA_BLOB: a DWORD length plus a raw pointer (never a C string)."""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


# Library names are constants so the loaders can be asserted in tests without
# pretending to be Windows.
_CRYPT32_LIBRARY = "crypt32"
_KERNEL32_LIBRARY = "kernel32"


def _load_crypt32(win_dll=None):
    """Loads crypt32.dll and pins the prototypes of the two functions used."""
    if win_dll is None and os.name != "nt":
        raise OSError("DPAPI is only available on Windows.")
    try:
        crypt32 = (win_dll or ctypes.WinDLL)(_CRYPT32_LIBRARY, use_last_error=True)
        blob_p = ctypes.POINTER(_Blob)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            function = getattr(crypt32, name)
            function.argtypes = [
                blob_p, ctypes.c_wchar_p, blob_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_uint32, blob_p,
            ]
            function.restype = ctypes.c_bool
        return crypt32
    except (OSError, AttributeError) as exc:
        raise OSError(f"Could not load DPAPI: {exc}") from exc


def _load_local_free(win_dll=None):
    """The freeing function for DPAPI output buffers.

    CryptProtectData/CryptUnprotectData allocate with LocalAlloc, so the buffer
    must be released with LocalFree - but LocalFree is exported by **kernel32**,
    not by crypt32. crypt32 re-exported it in older Windows builds, which is why
    calling crypt32.LocalFree appeared to work; newer builds (and therefore newer
    Python/Windows combinations) fail with
    ``AttributeError: function 'LocalFree' not found``. Ask kernel32, where the
    function is documented to live.
    """
    if win_dll is None and os.name != "nt":
        raise OSError("LocalFree is only available on Windows.")
    try:
        kernel32 = (win_dll or ctypes.WinDLL)(_KERNEL32_LIBRARY, use_last_error=True)
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        return local_free
    except (OSError, AttributeError) as exc:
        raise OSError(f"Could not load LocalFree: {exc}") from exc


def _last_windows_error() -> int:
    """WinError of the most recent crypt32 call (0 when ctypes cannot report it).

    ``ctypes.get_last_error`` only exists on Windows; the callers of this helper
    are already Windows-only, but the attribute lookup is guarded so the module
    stays importable and testable on every platform.
    """
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _free_blob_output(pointer, local_free=None) -> None:
    """Releases a buffer DPAPI allocated. Never raises, never hides a failure."""
    if not pointer:
        return
    try:
        (local_free or _load_local_free())(pointer)
    except Exception as exc:  # a leak must not lose the caller's session data
        logger.warning(f"Could not free a DPAPI buffer (leaked until exit): {exc}")


def _require_non_empty(data: bytes, operation: str) -> None:
    """Fail closed with a clear message instead of a ctypes ValueError."""
    if not data:
        raise OSError(f"Cannot {operation} an empty buffer with DPAPI.")


def _crypt_protect(crypt32, data: bytes, local_free=None) -> bytes:
    _require_non_empty(data, "protect")
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    plain = _Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    cipher = _Blob()
    if not crypt32.CryptProtectData(
        ctypes.byref(plain), None, None, None, None, 0, ctypes.byref(cipher)
    ):
        raise OSError(
            f"DPAPI CryptProtectData failed (Windows error {_last_windows_error()})."
        )
    try:
        return ctypes.string_at(cipher.pbData, cipher.cbData)
    finally:
        _free_blob_output(cipher.pbData, local_free)


def _crypt_unprotect(crypt32, blob: bytes, local_free=None) -> bytes:
    _require_non_empty(blob, "unprotect")
    buffer = ctypes.create_string_buffer(bytes(blob), len(blob))
    cipher = _Blob(len(blob), ctypes.cast(buffer, ctypes.c_void_p))
    plain = _Blob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(cipher), None, None, None, None, 0, ctypes.byref(plain)
    ):
        raise OSError(
            "DPAPI CryptUnprotectData failed "
            f"(Windows error {_last_windows_error()})."
        )
    try:
        return ctypes.string_at(plain.pbData, plain.cbData)
    finally:
        _free_blob_output(plain.pbData, local_free)


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
