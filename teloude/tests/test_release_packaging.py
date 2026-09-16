# teloude/tests/test_release_packaging.py
"""Static guards for the Windows release build assets.

These checks cannot prove that a compiled installer works on Windows - that is
`docs/installer_build_and_test.md` - but they fail the moment a build asset, a
version number or the "never commit credentials" rule regresses.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ISS_PATH = REPO_ROOT / "installer" / "teloude.iss"
SPEC_PATH = REPO_ROOT / "teloude.spec"
PS1_PATH = REPO_ROOT / "scripts" / "build_windows.ps1"
BAT_PATH = REPO_ROOT / "scripts" / "build_windows.bat"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iss_define(name: str) -> str:
    match = re.search(r'^#define\s+' + name + r'\s+"([^"]*)"', _read(ISS_PATH), re.M)
    assert match, f"installer/teloude.iss is missing #define {name}"
    return match.group(1)


class TestPackagingAssets:
    """The three build assets exist and still describe the release we ship."""

    def test_every_asset_is_present(self):
        for path in (ISS_PATH, SPEC_PATH, PS1_PATH, BAT_PATH, REPO_ROOT / "assets" / "icon.ico"):
            assert path.is_file(), f"missing release asset: {path}"

    def test_the_spec_produces_a_windowed_bundle(self):
        text = _read(SPEC_PATH)
        assert 'name="Teloude"' in text
        assert "console=False" in text, "a console window would appear on every start"
        assert "build_credentials.json" in text

    def test_the_installer_is_a_valid_per_user_install(self):
        text = _read(ISS_PATH)
        assert "[Setup]" in text and "[Files]" in text and "[Icons]" in text
        assert "PrivilegesRequired=lowest" in text, "a per-user install must not need admin"
        assert re.search(r"^AppId=\{\{[0-9A-Fa-f-]{36}\}\}$", text, re.M), (
            "AppId must be a GUID: it identifies the product for upgrades and uninstall"
        )
        assert "MinVersion=10.0" in text, "spec section 3: Windows 10/11"
        assert r"..\dist\Teloude\*" in text, "the bundle directory must be what gets installed"

    def test_the_installer_creates_both_shortcuts_and_optional_autostart(self):
        text = _read(ISS_PATH)
        assert r'Name: "{group}\{#MyAppName}"' in text, "Start Menu shortcut"
        assert "{autodesktop}" in text and "Tasks: desktopicon" in text, "optional desktop shortcut"
        assert "Tasks: autostart" in text, "optional start-with-Windows entry"
        assert "--minimized" in text, "autostart must not pop the window open"

    def test_uninstall_never_deletes_user_data(self):
        text = _read(ISS_PATH)
        # Only directives count: the comments explain what must NOT be deleted.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith(";")
        )
        deletes = re.findall(r"^Type:\s*filesandordirs?;\s*Name:\s*\"([^\"]+)\"", code, re.M)
        assert deletes == [r"{app}"], f"uninstall may only remove the program directory: {deletes}"
        for forbidden in ("{userappdata}", "{userdocs}", "{appdata}", r"Teloude\Teloude"):
            assert forbidden not in code, f"uninstall must never reference {forbidden}"
        # The data directory is named explicitly in the comment that explains why.
        assert r"%APPDATA%\Teloude" in text

    @pytest.mark.parametrize("name", ["MyAppVersion"])
    def test_the_installer_version_matches_the_project(self, name):
        project = re.search(r'^version\s*=\s*"([^"]+)"', _read(PYPROJECT_PATH), re.M).group(1)
        assert _iss_define(name) == project, "installer and package versions drifted apart"

    def test_the_build_script_is_plain_ascii(self):
        # PowerShell 5.1 misreads UTF-8 without a BOM on some systems; keeping the
        # script ASCII-only avoids that class of "cannot run the build" reports.
        for path in (PS1_PATH, BAT_PATH):
            data = path.read_bytes()
            data.decode("ascii")
            assert not data.startswith(b"\xef\xbb\xbf"), f"{path.name} must not carry a BOM"

    def test_the_build_script_runs_both_toolchains_and_cleans_up(self):
        text = _read(PS1_PATH)
        assert "PyInstaller" in text and "teloude.spec" in text
        assert "Installer" in text and "ISCC" in text and r"installer\teloude.iss" in text
        assert "build_credentials.json" in text
        assert "Remove-Item -Force $credentialsFile" in text, (
            "the generated credentials file must not be left in the working tree"
        )
        assert r"installer\Output\Teloude-Setup-" in text

    def test_credentials_are_gitignored(self):
        ignore = _read(REPO_ROOT / ".gitignore")
        assert "build_credentials.json" in ignore
        assert not (REPO_ROOT / "installer" / "build_credentials.json").exists(), (
            "a real build credentials file must never be committed"
        )
