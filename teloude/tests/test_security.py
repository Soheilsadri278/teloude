# teloude/tests/test_security.py
"""Security module tests: honest mechanisms, no fake encryption claims."""
import ctypes
import logging
import os

import pytest

from teloude.infrastructure.security.dpapi import (
    DpapiProtector,
    PlaintextProtector,
    SecureSessionStore,
    default_protector,
)


class TestPlaintextProtector:
    def test_roundtrip(self):
        protector = PlaintextProtector()
        assert protector.unprotect(protector.protect(b"session-bytes")) == b"session-bytes"

    def test_rejects_foreign_blobs(self):
        with pytest.raises(ValueError):
            PlaintextProtector().unprotect(b"garbage")

    def test_describe_is_honest(self):
        assert "NOT encrypted" in PlaintextProtector().describe()


class TestDpapiAvailability:
    def test_raises_off_windows(self):
        if os.name == "nt":
            pytest.skip("Windows-only negative test")
        with pytest.raises(OSError):
            DpapiProtector()

    def test_default_off_windows_is_plaintext(self):
        if os.name == "nt":
            pytest.skip("non-Windows behavior test")
        assert isinstance(default_protector(), PlaintextProtector)

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows DPAPI")
    def test_dpapi_roundtrip_on_windows(self):
        protector = DpapiProtector()
        assert protector.unprotect(protector.protect(b"abc")) == b"abc"
        assert "DPAPI" in protector.describe()


class _Recorder:
    """Callable that records the pointers it was asked to free."""

    def __init__(self):
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, pointer):
        self.calls.append(pointer)
        return None


class _FakeCrypt32:
    """Minimal crypt32 double: "encrypts" by copying into a fresh allocation.

    Each call hands back a real ctypes buffer, exactly like DPAPI hands back a
    LocalAlloc'd buffer, so the caller's freeing path is exercised for real.
    """

    def __init__(self):
        self.inputs = []
        self.allocated = []
        self._buffers = []

    def CryptProtectData(self, in_blob, description, entropy, reserved, prompt,
                         flags, out_blob):
        return self._copy(in_blob, out_blob)

    def CryptUnprotectData(self, in_blob, description, entropy, reserved, prompt,
                           flags, out_blob):
        return self._copy(in_blob, out_blob)

    @staticmethod
    def _structure(argument):
        """Resolves a DATA_BLOB argument.

        A real DLL receives the raw address; a Python double receives the
        ``ctypes.byref(...)`` object, which carries the structure it points at in
        ``_obj``. Accept both so the double can read what the caller wrote.
        """
        from teloude.infrastructure.security import dpapi

        target = getattr(argument, "_obj", argument)
        if isinstance(target, dpapi._Blob):
            return target
        return ctypes.cast(argument, ctypes.POINTER(dpapi._Blob)).contents

    def _copy(self, in_blob, out_blob):
        source = self._structure(in_blob)
        target = self._structure(out_blob)
        payload = ctypes.string_at(source.pbData, source.cbData)
        self.inputs.append(payload)
        buffer = ctypes.create_string_buffer(payload, len(payload))
        pointer = ctypes.cast(buffer, ctypes.c_void_p).value
        self._buffers.append(buffer)  # keep the fake allocation alive
        self.allocated.append(pointer)
        target.cbData = len(payload)
        target.pbData = pointer
        return True


class TestDpapiBufferOwnership:
    """DPAPI output buffers are LocalAlloc'd, so LocalFree must release them.

    LocalFree lives in kernel32. crypt32 re-exported it in older Windows builds,
    which is why ``crypt32.LocalFree`` appeared to work, but on newer builds the
    lookup fails with "AttributeError: function 'LocalFree' not found" (seen with
    Python 3.14 on Windows). These tests pin the loading rule and the pointer
    handling; production still loads the real system libraries.
    """

    def test_local_free_is_loaded_from_kernel32_with_a_pointer_prototype(self):
        from teloude.infrastructure.security import dpapi

        assert dpapi._KERNEL32_LIBRARY == "kernel32"
        assert dpapi._CRYPT32_LIBRARY == "crypt32"

        loaded = {}

        class _FakeKernel32:
            def __init__(self):
                self.LocalFree = _Recorder()

        def fake_win_dll(name, use_last_error=False):
            loaded["name"] = name
            loaded["use_last_error"] = use_last_error
            return _FakeKernel32()

        local_free = dpapi._load_local_free(win_dll=fake_win_dll)
        assert loaded["name"] == "kernel32"  # not crypt32
        assert loaded["use_last_error"] is True
        assert local_free.argtypes == [ctypes.c_void_p]  # survives 64-bit pointers
        assert local_free.restype is ctypes.c_void_p

    def test_data_blob_uses_a_raw_pointer_not_a_c_string(self):
        from teloude.infrastructure.security import dpapi

        fields = dict(dpapi._Blob._fields_)
        assert fields["pbData"] is ctypes.c_void_p
        assert fields["cbData"] is ctypes.c_uint32

    def test_protect_and_unprotect_free_every_allocated_buffer(self):
        library = _FakeCrypt32()
        free = _Recorder()
        protector = DpapiProtector(crypt32=library, local_free=free)

        blob = protector.protect(b"session-bytes")
        assert protector.unprotect(blob) == b"session-bytes"

        assert library.inputs == [b"session-bytes", b"session-bytes"]
        # the exact pointers DPAPI allocated, released once each
        assert free.calls == library.allocated
        assert len(free.calls) == 2

    def test_binary_payload_survives_with_embedded_nul_bytes(self):
        library = _FakeCrypt32()
        free = _Recorder()
        protector = DpapiProtector(crypt32=library, local_free=free)

        payload = bytes(range(256)) + b"\x00\x00tail"
        assert protector.unprotect(protector.protect(payload)) == payload

    def test_a_failing_free_is_logged_and_still_returns_the_data(self, caplog):
        library = _FakeCrypt32()

        def broken_free(pointer):
            raise OSError("kernel32 refused")

        protector = DpapiProtector(crypt32=library, local_free=broken_free)
        with caplog.at_level(logging.WARNING):
            blob = protector.protect(b"payload")
        assert blob == b"payload"  # the caller never loses its session data
        assert "Could not free" in caplog.text
        assert "kernel32 refused" in caplog.text

    def test_no_pointer_means_nothing_to_free(self):
        from teloude.infrastructure.security import dpapi

        free = _Recorder()
        dpapi._free_blob_output(None, free)
        dpapi._free_blob_output(0, free)
        assert free.calls == []

    def test_empty_input_fails_closed_with_a_clear_message(self):
        protector = DpapiProtector(crypt32=_FakeCrypt32(), local_free=_Recorder())
        with pytest.raises(OSError) as excinfo:
            protector.unprotect(b"")
        assert "empty buffer" in str(excinfo.value)

    def test_dpapi_failure_raises_with_the_windows_error_code(self):
        class _FailingCrypt32:
            def CryptProtectData(self, *args):
                return False

            CryptUnprotectData = CryptProtectData

        protector = DpapiProtector(
            crypt32=_FailingCrypt32(), local_free=_Recorder()
        )
        with pytest.raises(OSError) as excinfo:
            protector.protect(b"data")
        assert "CryptProtectData failed" in str(excinfo.value)


class TestSecureSessionStore:
    def test_lock_without_file_is_noop(self, tmp_path):
        store = SecureSessionStore(
            tmp_path / "s.session", protector=PlaintextProtector()
        )
        assert store.lock() is False
        assert store.is_locked() is False

    def test_lock_unlock_roundtrip(self, tmp_path):
        session = tmp_path / "s.session"
        session.write_bytes(b"fake-session-bytes")
        store = SecureSessionStore(session, protector=PlaintextProtector())
        assert store.lock() is True
        assert store.is_locked() is True
        assert not session.exists()  # plaintext removed while locked
        assert store.unlock() is True
        assert session.read_bytes() == b"fake-session-bytes"
        assert store.is_locked() is False

    def test_unlock_fresh_is_ok(self, tmp_path):
        store = SecureSessionStore(
            tmp_path / "missing.session", protector=PlaintextProtector()
        )
        assert store.unlock() is True

    def test_corrupt_lock_fails_closed(self, tmp_path):
        session = tmp_path / "s.session"
        locked = tmp_path / "s.session.locked"
        locked.write_bytes(b"corrupted-blob")
        store = SecureSessionStore(session, protector=PlaintextProtector())
        assert store.unlock() is False
        assert not session.exists()  # no partial plaintext left behind
