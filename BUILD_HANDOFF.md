# Teloude — Windows Release Build & Handoff

## 1. Executive Summary & Git State

- **Repository**: `Soheilsadri278/teloude`
- **Current Branch**: `arena/01a0b4a0-teloude`
- **Pushed to Remote**: Yes (`git push -u origin arena/01a0b4a0-teloude`)
- **Canonical HEAD Commit**: `2032b87a7f0bca87b0ac46687bb737b89f1846ad` (`Merge pull request #1 from Soheilsadri278/arena/01a0b0d3-teloude`)
- **Note on Commit `13ede91`**:
  An exhaustive search across git history, reflog, all remote branches (`main`, `arena/01a0b0d3-teloude`, `fix/windows-ci-test-hang`, `phase-1.2-telethon-adapter`, `phase-1.3-session-auth`), tags (`v1.0.0`, `v1.3.0-before-ui-redesign`), and the GitHub API confirms that commit `13ede91` does not exist in `Soheilsadri278/teloude`.
  The canonical HEAD containing all v1 features, UI redesign, MTProxy support, upload speed optimizations, official logo rollout, and Windows CI fixes is commit **`2032b87`** on `main` (and tracked by our current session branch `arena/01a0b4a0-teloude`).

---

## 2. Latest Windows Release Build & Artifact Download

The official Windows release build workflow (`.github/workflows/windows-release.yml`) was executed on GitHub Actions (`windows-latest`) for commit `2032b87` and completed with **100% success**.

### Artifact Details

| Item | Details |
| --- | --- |
| **Workflow Run** | [Windows release build #35342811872](https://github.com/Soheilsadri278/teloude/actions/runs/35342811872) |
| **Workflow Status** | `completed` / `success` (Duration: 9 minutes 38 seconds) |
| **Commit Built** | `2032b87a7f0bca87b0ac46687bb737b89f1846ad` (`main`) |
| **Artifact Name** | `Teloude-Setup-0.1.0-2032b87` |
| **Artifact ID** | `10545786593` |
| **File Size** | `67,764,044 bytes (~64.6 MB)` |
| **SHA-256 Digest** | `ac5347ebf200daaf8badb35e4509e02b5244d4cdbed43c12b585a9d29e5db4f6` |
| **Expiration Date** | `2026-10-18` |

### Download Links

- **Direct Artifact Download (ZIP)**:
  [https://github.com/Soheilsadri278/teloude/actions/runs/35342811872/artifacts/10545786593](https://github.com/Soheilsadri278/teloude/actions/runs/35342811872/artifacts/10545786593)
- **GitHub Actions Run Overview Page**:
  [https://github.com/Soheilsadri278/teloude/actions/runs/35342811872](https://github.com/Soheilsadri278/teloude/actions/runs/35342811872)

*(Note: Downloading GitHub Actions artifacts requires being logged into a GitHub account with read access to the repository).*

---

## 3. Contents of the Artifact

The downloaded `Teloude-Setup-0.1.0-2032b87.zip` contains:

1. **`Teloude-Setup-0.1.0.exe`**:
   The standalone per-user Windows installer built with Inno Setup 7.1.0. It packages the bundled `dist\Teloude\Teloude.exe`, Python 3.12 64-bit runtime, PySide6, Telethon, DPAPI security module, and application assets.
2. **`Teloude-Setup-0.1.0.exe.sha256`**:
   The SHA-256 checksum file to verify download integrity.
3. **`build-info.txt`**:
   Contains commit SHA, build timestamp, toolchain versions (Python 3.12, Inno Setup 7.1.0, PyInstaller).
4. **`pip-freeze.txt`**:
   Complete list of exact package versions installed on the build runner.
5. **`icon-teloude-exe.png` & `icon-teloude-installer.png`**:
   Extracted icon bitmaps from the built binaries, verified against the official logo (`assets/icon-2.png`) by the CI verification step.

---

## 4. Key Features Delivered in This Build

- **Official Logo Rollout**:
  `assets/icon-2.png` is the official logo. `assets/icon.ico` (16–256 px) is embedded in `Teloude.exe`, the installer, the taskbar (`AppUserModelID`), Alt-Tab, window headers, and the system tray.
- **Translucent Apple Glass Proxy Dialog**:
  Full MTProto proxy support with an animated 4-state indicator, OS-level window glass translucency (`WA_TranslucentBackground`), secret masking/unmasking, and on-the-wire validation.
- **High-Speed Pipelined Uploads**:
  Configurable 512 KiB MTProto chunks with an 8-part in-flight pipelining window, accelerating transfer speeds up to 50+ MB/s (~23x faster than sequential 128 KiB transfers) while preserving crash recovery and resume checkpoints.
- **Desktop Tray Notifications**:
  Non-intrusive system notifications upon completion or failure of backup and restore operations.
- **Bulletproof Windows Test Suite**:
  Hang watchdog timers, platform-specific process reaping assertions, and JUnit reporting (619 passed, 1 skipped).

---

## 5. How to Build Locally on Windows 10/11

If building directly on a Windows workstation or VM:

### Prerequisites
- Python 3.9+ 64-bit (Python 3.12 recommended)
- Inno Setup 6.3+ or 7.x installed (e.g. `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` or `C:\InnoSetup7\ISCC.exe`)
- Dependencies installed: `python -m pip install -e ".[dev]" pyinstaller`

### One-Command Build
From the repository root in PowerShell:

```powershell
$env:TELOUDE_API_ID  = "<your_telegram_api_id>"
$env:TELOUDE_API_HASH = "<your_telegram_api_hash>"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SmokeTest
```

Output binary:
`installer\Output\Teloude-Setup-0.1.0.exe`

---

## 6. Testing & Acceptance Checklist

To verify the installed application on a clean Windows machine:
1. **Python-free check**: Verify `where python` returns nothing.
2. **Install**: Run `Teloude-Setup-0.1.0.exe`. Verify lowest-privilege per-user installation into `%LOCALAPPDATA%\Programs\Teloude` without UAC prompt.
3. **Run**: Launch from Start Menu or desktop shortcut. Verify sign-in wizard appears (no console/terminal window).
4. **Proxy**: Click the connection indicator, test MTProto proxy connection.
5. **Data persistence**: Verify `%APPDATA%\Teloude` holds `teloude_data.db` and sessions.
6. **Uninstall**: Uninstall from Windows Settings > Apps; verify program directory is deleted while user databases/sessions in `%APPDATA%\Teloude` are preserved.

For the detailed acceptance checklist, refer to:
- `docs/installer_build_and_test.md`
- `docs/live_acceptance_checklist.md`
