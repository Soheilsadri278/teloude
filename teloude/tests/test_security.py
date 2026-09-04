# teloude/tests/test_security.py
"""Security module tests: honest mechanisms, no fake encryption claims."""
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
