# Building and testing the Windows installer

Teloude ships as a **per-user Windows installer that contains its own Python
runtime**. A user installs it and runs it: no Python, no pip, no compiler, no
terminal (spec sections 3, 11 and phase 9).

The installer is produced from the sources in this repository in two steps:

```text
PyInstaller  ->  dist\Teloude\Teloude.exe     (application + Python runtime + dependencies)
Inno Setup 6/7 ->  installer\Output\Teloude-Setup-<version>.exe   (the installer a user runs)
```

The Windows binaries cannot be produced on Linux or macOS: PyInstaller never
cross-compiles (it bundles the bootloader and the libraries of the machine it
runs on) and Inno Setup is Windows-only. Run the build on Windows 10/11.

## 1. One command

From the repository root (or double-click `scripts\build_windows.bat`):

```powershell
$env:TELOUDE_API_ID  = "<your api id>"
$env:TELOUDE_API_HASH = "<your api hash>"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SmokeTest
```

**Output (this is the file to hand to a user):**

```text
installer\Output\Teloude-Setup-0.1.0.exe
```

The script prints that absolute path, the file size and the SHA-256 when it
finishes, and it removes the generated credentials file from the working tree
again. A failed prerequisite stops the build with the exact command to fix it.

### Prerequisites (build machine only)

| Tool | Why | Where |
| --- | --- | --- |
| Python 3.9+ 64-bit | to build and to run the tests | <https://www.python.org/downloads/windows/> |
| `pip install -e ".[dev]"` | PySide6, Telethon, pydantic, pillow, pytest, ruff, **PyInstaller** | in the repository |
| Inno Setup 6.3+ or 7.x | compiles `installer\teloude.iss` into the installer (`x64compatible` needs 6.3; the CI build uses the 7.1.0 x64 edition) | <https://jrsoftware.org/isdl.php> |

On Python 3.14, PySide6 must be **6.10.1 or newer** (the first release with 3.14
wheels); `pip install -e ".[dev]"` resolves a suitable version, an older pinned
`pip install PySide6` does not.

### Options

```text
-SmokeTest               build, then start the packaged app offline for 20s and
                         check that it stayed up and logged no traceback
-SkipPyInstaller         reuse the existing dist\Teloude and only recompile the installer
-NoClean                 keep the PyInstaller cache (faster rebuilds)
-AllowUnconfiguredBuild  internal test build: the app then needs
                         TELOUDE_API_ID / TELOUDE_API_HASH in its environment
-InnoSetupPath <path>    ISCC.exe when Inno Setup is not in its default location
```

By hand, the same two steps are:

```bat
python -m PyInstaller teloude.spec
iscc installer\teloude.iss
```

## 2. What the installer does

* Installs **per user** into `%LOCALAPPDATA%\Programs\Teloude` — no
  administrator prompt, no UAC (`PrivilegesRequired=lowest`).
* **Start Menu**: `Teloude` and `Uninstall Teloude`.
* **Desktop shortcut**: offered as an optional task (unchecked by default).
* **Start with Windows**: optional task, unchecked by default; writes one
  `HKCU\...\CurrentVersion\Run` value that launches `Teloude.exe --minimized`,
  so the app starts hidden in the tray (spec section 29).
* **Add/Remove Programs** entry with version, publisher and icon.
* **Upgrades**: a stable `AppId` makes a newer installer replace an older one in
  place, keeping the install directory and the user's data
  (`CloseApplications=yes`, `UsePreviousDir=yes`).
* **Uninstall keeps user data.** The program directory is removed; the database,
  logs, preview cache and the DPAPI-protected Telegram session in
  `%APPDATA%\Teloude` are deliberately left untouched (spec sections 11, 20:
  cloud cleanup must never imply local cleanup, and the local index describes
  backups that live in the user's Telegram account).
* **No terminal**: the packaged app is a windowed executable (`console=False`).

## 3. Where the credentials come from

Spec section 11 requires Teloude to use **its own registered Telegram API
credentials** and says the user should not be asked for them. A user who
double-clicks a Start Menu shortcut has no environment variables, so the release
build bakes them in:

1. `scripts\build_windows.ps1` writes `TELOUDE_API_ID` / `TELOUDE_API_HASH` from
   the builder's environment to `installer\build_credentials.json`;
2. `teloude.spec` ships that file inside the bundle
   (`dist\Teloude\_internal\build_credentials.json`);
3. `teloude/config.py` uses the environment first (development, tests, support
   overrides) and falls back to the bundled file;
4. the script deletes the file from the working tree again.

`build_credentials.json` is gitignored and must never be committed: the
credentials belong to the released application, not to the repository or its
history. Values are never logged. A build without them still works but starts
only when both environment variables are set (`-AllowUnconfiguredBuild`), which
is what the test suite uses and what keeps an unconfigured process exiting with
code 2 and an actionable message.

## 4. Testing the installer on Windows

Run this on a Windows 10/11 machine — ideally a VM or the account of a person who
has never installed Python. Steps 1–6 are the release acceptance checks.

```text
1. Python-free install
   - Verify Python is really absent:  where python      (should print nothing)
   - Copy Teloude-Setup-0.1.0.exe over, double-click it.
   - Expect: no administrator prompt, no Python, no pip, no terminal window.
     It installs into %LOCALAPPDATA%\Programs\Teloude.

2. Run the application
   - Start Menu -> Teloude (and the desktop shortcut if it was ticked).
   - Expect: the sign-in wizard appears (no console window anywhere), phone
     number -> code -> 2FA -> connected. The tray icon appears.
   - Close the window: the app keeps running in the tray (spec section 29).

3. Check the shortcuts
   - %APPDATA%\Microsoft\Windows\Start Menu\Programs\Teloude contains Teloude
     and Uninstall Teloude.
   - Optional tasks: uninstall and reinstall ticking "desktop shortcut" and
     "start with Windows"; then check
       HKCU\Software\Microsoft\Windows\CurrentVersion\Run  ->  Teloude
     points at <install dir>\Teloude.exe --minimized, and that a sign-out /
     sign-in starts Teloude hidden in the tray.

4. Restart the application (data survives)
   - Quit from the tray, start again: it opens without asking to sign in
     (the session in %APPDATA%\Teloude\sessions is reused and DPAPI-unlocked).

5. Uninstall
   - Settings > Apps > Teloude > Uninstall (or Start Menu > Uninstall Teloude).
   - Expect: program directory removed, Start Menu entries removed, tray
     autostart value removed, no error.

6. User data survives the uninstall
   - Before and after the uninstall run:
       dir "%APPDATA%\Teloude"
       dir "%APPDATA%\Teloude\sessions"
   - Expect: teloude_data.db, logs\ and the session file are all still there,
     with their sizes unchanged. Nothing in the user's Telegram account changed.

7. Reinstall over the previous version (upgrade path)
   - Install an older Teloude-Setup-*.exe, then the newer one: no error, the
     install directory is reused, the data and the session are unchanged.
```

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Windows SmartScreen: "Windows protected your PC" | The installer is not code-signed. That is expected for a local build; see below. |
| `Inno Setup 6 is required` | Install it from <https://jrsoftware.org/isdl.php> or pass `-InnoSetupPath`. |
| `PySide6 is not installed` on Python 3.14 | older PySide6 has no 3.14 wheels: `python -m pip install -e ".[dev]"` (PySide6 >= 6.10.1). |
| Installed app shows "Teloude is not configured" | the build was made with `-AllowUnconfiguredBuild`, or without `TELOUDE_API_ID`/`TELOUDE_API_HASH`. Rebuild with the credentials set. |
| Installed app shows nothing at all | the reason is in `%APPDATA%\Teloude\logs\teloude.log`; a windowed build has no console. |
| Antivirus quarantines the installer | One-file/two-file PyInstaller bundles are sometimes flagged heuristically. Keep the onedir layout (`dist\Teloude`), sign the binaries, or submit the hash to the vendor. |

### Signing (optional, recommended for distribution)

An unsigned installer triggers SmartScreen on machines that have never seen it.
With an Authenticode certificate:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /a dist\Teloude\Teloude.exe
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /a installer\Output\Teloude-Setup-0.1.0.exe
```

Sign `Teloude.exe` before compiling the installer so the payload is signed too.

## 5. Building the release in GitHub Actions

`.github/workflows/windows-release.yml` runs the same build on a Windows
runner - no Windows machine, no installed toolchain and no local secrets:

```text
push (main, phase-*) or "Run workflow"
        |
        v
windows-latest:  Ruff -> pytest -> PyInstaller -> Inno Setup -> artifact
```

| Step | What it does |
| --- | --- |
| Check the build credentials | Fails in seconds, naming the missing secrets, instead of after a 20-minute build |
| Python 3.12 + `pip install -e ".[dev]"` | The application, PySide6, Telethon, pytest, Ruff, PyInstaller |
| Ruff, pytest | The same checks as locally (`QT_QPA_PLATFORM=offscreen` for the Qt tests) |
| Install Inno Setup 7.1.0 | Downloaded from the official immutable release and verified against the pinned SHA-256 before it is executed; the compiler is looked up in the install directory first and the log records which version ran |
| Build | `scripts\build_windows.ps1` - the same script as above, called with `-InnoSetupPath` |
| Artifact | The installer, its `.sha256`, `build-info.txt` (commit, toolchain, size, hash) and `pip-freeze.txt`; kept 30 days |

**Required repository secrets** (Settings > Secrets and variables > Actions >
New repository secret):

| Secret | Value |
| --- | --- |
| `TELOUDE_API_ID` | the `api_id` of Teloude's registered Telegram application |
| `TELOUDE_API_HASH` | the matching `api_hash` |

The values are passed to the build step as environment variables only, are
never written into the repository, never printed in a log and never part of the
artifact; the build script writes them to the gitignored
`installer/build_credentials.json`, PyInstaller ships that file inside the
bundle and the script deletes it again when it finishes. Without the two
secrets the workflow stops in its first step and says which one is missing.

Two manual (`Run workflow`) inputs exist for exceptions: *smoke_test* starts the
packaged application offline for 20 seconds and checks it stayed up (the
documented local build does this; it needs a desktop session on the runner), and
*allow_unconfigured_build* builds without baked-in credentials for internal
testing - that installer starts only when `TELOUDE_API_ID` / `TELOUDE_API_HASH`
are set in its environment, so it is never handed to a user.

The workflow uploads an **artifact**, never a GitHub Release: an unsigned
installer (SmartScreen warns about it) is not a release until a human has run
the checks in section 4 below. Automated tests cannot replace those checks -
they cover the application, not the installer, the shortcuts, DPAPI against the
real `crypt32` or the uninstall.

## 6. What cannot be verified outside Windows

The repository's automated tests (`python -m pytest -q`) cover the application,
the credential resolution and the static integrity of these build assets. They
cannot cover: running `ISCC`, Windows shortcut creation, DPAPI against the real
`crypt32`/`kernel32`, the tray on a real desktop, and the uninstall behaviour.
Those are the manual steps in section 4.
