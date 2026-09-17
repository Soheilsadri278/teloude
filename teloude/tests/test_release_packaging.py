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


WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "windows-release.yml"


class TestWindowsReleaseWorkflow:
    """The CI side of the release build (`.github/workflows/windows-release.yml`).

    Read as text on purpose: the release assets must stay checkable on any
    machine that can run the test suite, without a YAML parser or a runner.
    What the workflow *does* on a Windows runner cannot be checked here - that
    is a real workflow run - but its contract can: it runs the suite and Ruff
    before building, it takes the credentials from repository secrets, it uses
    the committed build script, and it hands over an artifact instead of
    publishing anything.
    """

    def test_it_exists_and_builds_on_a_windows_runner(self):
        text = _read(WORKFLOW_PATH)
        assert "runs-on: windows-latest" in text
        assert "workflow_dispatch:" in text, "the build must be runnable by hand"
        assert "push:" in text, "a pushed commit should build the installer"

    def test_it_verifies_the_code_before_building_it(self):
        text = _read(WORKFLOW_PATH)
        assert "python -m pytest -v" in text, (
            "the test suite must run in CI, naming each test as it starts"
        )
        assert "python -m ruff check teloude/" in text, "Ruff must run in CI"
        assert text.index("- name: Tests") < text.index(
            "- name: Build the bundle and the installer"
        ), "tests and Ruff must pass before anything is packaged"

    def test_a_test_that_never_finishes_fails_the_job_and_names_itself(self):
        """A blocked test must fail in seconds instead of stalling the job.

        A real run once sat in the Tests step for 42 minutes with no output and
        nothing naming the test that was stuck. Three things prevent a repeat,
        and all three are pinned here: every test is named as it starts (-v), the
        suite is armed with the per-test hang watchdog, and the watchdog report
        is printed whether the step passed or failed.
        """
        text = _read(WORKFLOW_PATH)
        tests_step = text[text.index("- name: Tests"):]
        tests_step = tests_step[:tests_step.index("- name: Install Inno Setup")]
        assert "TELOUDE_TEST_WATCHDOG: '30'" in tests_step, (
            "the per-test limit must stay short enough to end a hang in seconds"
        )
        assert "TELOUDE_TEST_GUARD: '45'" in tests_step, (
            "the supervisor must be armed as the backstop for a block Python cannot see"
        )
        assert "TELOUDE_TEST_HEARTBEAT: '1'" in tests_step, (
            "progress must be a heartbeat, so a slow test is not mistaken for a hang"
        )
        assert "TELOUDE_TEST_DUMP_GRACE: '3'" in tests_step, (
            "the diagnostic window must stay inside the supervisor limit"
        )
        assert "TELOUDE_TEST_HANG_GRACE: '5'" in tests_step, (
            "once a HANG is on record the supervisor must stop waiting out the "
            "whole guard window"
        )
        assert "TELOUDE_WATCHDOG_FILE" in tests_step, (
            "the watchdog report must have somewhere to go"
        )

        report_step = text[text.index("- name: Report a test that never finished"):]
        assert "if: always()" in report_step[:400], (
            "the report must be printed after a failed or cancelled test step"
        )
        assert "-TotalCount" in report_step, (
            "the report must show that the watchdogs were armed for this run"
        )
        # The watchdogs emit ::error:: annotations naming the test themselves; the
        # report step prints the same report, so the verdict is in both places.
        assert "Get-Content -LiteralPath $env:TELOUDE_WATCHDOG_FILE -Tail 200" in report_step
        assert text.index("- name: Report a test that never finished") < text.index(
            "- name: Build the bundle and the installer"
        ), "the report is part of the verification, before anything is packaged"

    def test_it_uses_the_committed_build_script(self):
        text = _read(WORKFLOW_PATH)
        assert "build_windows.ps1" in text, "CI must not re-implement the build"
        assert "InnoSetupPath" in text, "the compiler location is passed to the script"

    def test_the_credentials_come_from_secrets_and_are_never_written_out(self):
        text = _read(WORKFLOW_PATH)
        assert "${{ secrets.TELOUDE_API_ID }}" in text
        assert "${{ secrets.TELOUDE_API_HASH }}" in text
        # ...and nothing that looks like a credential value, anywhere.
        for line in text.splitlines():
            if line.strip().startswith("#") or "INNO_SETUP_SHA256" in line:
                continue  # the pinned, public compiler checksum is not a secret
            assert not re.search(r"\b[0-9a-f]{32,}\b", line), f"literal secret in: {line}"
        assert not re.search(r"(Write-Host|Write-Output|echo)[^\n]*\$env:TELOUDE_API", text), (
            "the credentials must never be printed"
        )

    def test_it_uploads_the_installer_as_an_artifact(self):
        text = _read(WORKFLOW_PATH)
        assert "actions/upload-artifact@" in text
        assert "installer/Output/Teloude-Setup-*.exe" in text
        assert "if-no-files-found: error" in text, "a build without an installer must fail"

    def test_it_publishes_no_release(self):
        """An unsigned installer is not a release until a human has run the
        acceptance checklist, so this workflow must not create one by itself."""
        text = _read(WORKFLOW_PATH)
        for forbidden in ("softprops/action-gh-release", "gh release create", "contents: write"):
            assert forbidden not in text, f"{forbidden} would publish by itself"

    def test_the_pinned_installer_compiler_is_checksum_verified(self):
        text = _read(WORKFLOW_PATH)
        match = re.search(r"INNO_SETUP_SHA256: '([0-9a-f]{64})'", text)
        assert match, "the Inno Setup download must be pinned to a SHA-256"
        assert "Get-FileHash -Algorithm SHA256" in text, "the download must be verified"
        assert re.search(r"INNO_SETUP_URL: 'https://github\.com/jrsoftware/issrc/releases/", text), (
            "use the official immutable release, not a mirror"
        )
