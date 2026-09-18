# teloude/tests/test_build_credentials.py
"""The credentials a release build bakes in, and how a packaged app explains
itself when they are missing (spec section 11).

The repository never contains the values: the build script writes them to a
gitignored file, the spec ships that file inside the bundle, and this module
pins the resolution order (environment first, bundle second), the degradation to
"not configured", and the rule that values never reach the logs.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

from teloude import config
from teloude.config import _credentials_from_build, build_credentials_path


class TestBuildCredentials:
    """The release build ships credentials; the repository never does."""

    def _write(self, tmp_path: Path, payload) -> Path:
        path = tmp_path / "build_credentials.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return path

    def test_a_bundle_file_supplies_the_credentials(self, tmp_path):
        path = self._write(tmp_path, {"api_id": 424242, "api_hash": "a" * 32})
        assert _credentials_from_build(path) == (424242, "a" * 32)

    def test_no_file_is_the_old_unconfigured_state(self, tmp_path):
        assert _credentials_from_build(tmp_path / "missing.json") == (0, "")

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            "[]",
            '{"api_id": 1}',
            '{"api_id": "x", "api_hash": "y"}',
            '{"api_id": 0, "api_hash": "y"}',
            '{"api_id": 7, "api_hash": ""}',
        ],
    )
    def test_a_broken_file_degrades_to_unconfigured(self, tmp_path, payload):
        assert _credentials_from_build(self._write(tmp_path, payload)) == (0, "")

    def test_the_values_are_never_logged(self, tmp_path, caplog):
        secret_hash = "b" * 32
        path = self._write(tmp_path, {"api_id": 424242, "api_hash": secret_hash})
        with caplog.at_level(logging.DEBUG):
            assert _credentials_from_build(path) == (424242, secret_hash)
        assert secret_hash not in caplog.text
        assert "424242" not in caplog.text

    def test_a_source_checkout_never_reads_build_credentials(self, monkeypatch):
        # Not frozen: the developer machine must be configured explicitly, so it
        # can never sign in as the released application by accident.
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert build_credentials_path() is None

    def test_a_frozen_build_reads_them_from_the_bundle(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert build_credentials_path() == tmp_path / "build_credentials.json"

    def test_the_environment_still_wins(self, monkeypatch):
        # Support and development override a released build without rebuilding it.
        monkeypatch.setenv("TELOUDE_API_ID", "999")
        monkeypatch.setenv("TELOUDE_API_HASH", "c" * 32)
        assert config._resolve_api_credentials() == (999, "c" * 32)

    def test_the_environment_completes_a_half_configured_build(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELOUDE_API_ID", "999")
        monkeypatch.delenv("TELOUDE_API_HASH", raising=False)
        monkeypatch.setattr(
            config, "_credentials_from_build",
            lambda path=None: (111, "d" * 32),
        )
        assert config._resolve_api_credentials() == (999, "d" * 32)


class TestStartupDialog:
    """A packaged build explains itself in a window - where a user is looking."""

    @staticmethod
    def _gate(monkeypatch, frozen, platform):
        from teloude.ui import app as ui_app

        if frozen:
            monkeypatch.setattr(sys, "frozen", True, raising=False)
        else:
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "platform", platform)
        return ui_app._can_show_startup_dialog()

    def test_a_source_checkout_never_opens_a_window(self, monkeypatch):
        assert self._gate(monkeypatch, frozen=False, platform="win32") is False

    def test_the_installed_windows_build_does(self, monkeypatch):
        assert self._gate(monkeypatch, frozen=True, platform="win32") is True

    def test_a_headless_or_scripted_run_does_not_block_on_a_dialog(self, monkeypatch):
        # Frozen on Linux (containers, CI, smoke tests): keep printing and exit
        # with the documented code instead of waiting for a click nobody makes.
        assert self._gate(monkeypatch, frozen=True, platform="linux") is False
