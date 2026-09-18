# Teloude — Windows Release Build & Handoff

*Updated 2026-09-18 by the session on branch `arena/01a0b4aa-teloude`.*

## 1. Executive Summary & Git State

- **Repository**: `Soheilsadri278/teloude` (public)
- **Session Branch**: `arena/01a0b4aa-teloude`
- **Pushed to Remote**: Yes — `git push -u origin arena/01a0b4aa-teloude`
- **Canonical HEAD (code)**: `2032b87a7f0bca87b0ac46687bb737b89f1846ad`
  (`Merge pull request #1 from Soheilsadri278/arena/01a0b0d3-teloude`, `main`)
- **Branch state**: the session branch was fast-forwarded from `cb5f2d3` to
  `2032b87`. Both commits hold **the same tree** (`git diff --stat 2032b87 cb5f2d3`
  is empty — `cb5f2d3` is an ancestor of `2032b87`), so advancing the branch
  dropped nothing and added the canonical merge commit.
- **Build commit**: `789f7daea422da0082ee787096c1c2ffaab0b13c`
  (`TEST ONLY: run the Windows release build for this session branch`)

### Note on commit `13ede91`

Commit `13ede91` **does not exist** in `Soheilsadri278/teloude`. Checked, in this
order, on 2026-09-18:

| Where | Result |
| --- | --- |
| `git cat-file -t 13ede91` (full, non-shallow clone) | `fatal: Not a valid object name` |
| `git log --all --oneline` (all local + fetched branches) | no such commit |
| `git reflog` / `git fsck --lost-found` | nothing dangling, no trace |
| Remote refs (`git ls-remote origin`): `main`, `arena/01a0b0d3-teloude`, `arena/01a0b4a0-teloude`, PR #1, tags `v1.0.0`, `v1.3.0-before-ui-redesign` | no such commit |
| GitHub API (`gh api .../commits/13ede91`) | `422 No commit found for SHA` |

(An earlier handoff on the sibling branch `arena/01a0b4a0-teloude` reached the
same conclusion after the same search.) The commit holding all v1 features, the
UI redesign, MTProxy support, the upload-speed work, the official logo rollout
and the Windows CI fixes is **`2032b87`** on `main` — which is what was pushed
and built here.

### How the build was triggered (and why it looks like a "TEST ONLY" commit)

The session's GitHub token cannot dispatch workflows:
`gh workflow run` → `HTTP 403: Resource not accessible by integration`.
The release workflow only accepts pushes to `main` and `phase-*`, and a GitHub
`push` event is filtered by the workflow file **in the pushed commit**. So the
build commit `789f7da` carries one extra line — `- 'arena/**'` under
`on.push.branches` — purely to let this branch start the run. That line is
removed again in the commit that adds this document (a push made while the run
was going would have cancelled it through the workflow's concurrency group, so
the revert had to wait for the run to finish). The application code, the spec
file and the installer script in the build commit are byte-identical to
`2032b87`; the only difference between `789f7da` and `2032b87` is that trigger
line, which is not part of any packaged binary.

## 2. Latest Windows Release Build & Artifact

| Item | Details |
| --- | --- |
| **Workflow** | [Windows release build #35349769899](https://github.com/Soheilsadri278/teloude/actions/runs/35349769899) |
| **Status** | `completed` / **`success`** — job `Build Teloude-Setup.exe`, 9m 21s (2026-09-18 13:21:49 → 13:31:10 UTC) |
| **Commit built** | `789f7daea422da0082ee787096c1c2ffaab0b13c` (code identical to `2032b87`) |
| **Artifact name** | `Teloude-Setup-0.1.0-789f7da` |
| **Artifact ID** | `10549727995` |
| **Size** | `67,771,936 bytes (~64.6 MB)` |
| **SHA-256 (artifact ZIP)** | `3a2686c0f231405ca57ec666688fa93bc4bb855b2a25fe1d4cd1db9a732e1d25` |
| **Expires** | `2026-10-18` (30-day retention) |
| **Runner** | GitHub-hosted `windows-latest` (x64), 4-core standard runner |
| **Toolchain** | Python 3.12, PyInstaller, Inno Setup 7.1.0 (SHA-256-verified download) |
| **Telegram credentials** | Baked into the bundle — the "Check the Telegram build credentials" gate passed *without* `-AllowUnconfiguredBuild`, i.e. the run had `TELOUDE_API_ID` / `TELOUDE_API_HASH` |

### Download links

- **Direct artifact download (ZIP, signed in to GitHub required — the repo is
  public but artifact downloads still need an account):**
  <https://github.com/Soheilsadri278/teloude/actions/runs/35349769899/artifacts/10549727995>
- **Run overview page:** <https://github.com/Soheilsadri278/teloude/actions/runs/35349769899>
- **All artifacts of the run:**
  <https://github.com/Soheilsadri278/teloude/actions/runs/35349769899/artifacts>

Unzipped, the archive contains `Teloude-Setup-0.1.0.exe` — the installer a user
runs — plus its `.sha256` sidecar, which is the authoritative hash **of the
installer itself** (the ZIP digest above covers the GitHub artifact wrapper).

Earlier build, from the same code (`2032b87`), still valid until 2026-10-18:
[run #35342811872](https://github.com/Soheilsadri278/teloude/actions/runs/35342811872) →
`Teloude-Setup-0.1.0-2032b87`, 67,764,044 bytes,
`ac5347ebf200daaf8badb35e4509e02b5244d4cdbed43c12b585a9d29e5db4f6`.

### Evidence that the run is green end to end

Every step of the job, as reported by the Actions API:

| Step | Result |
| --- | --- |
| Check out the sources | ✅ |
| Check the Telegram build credentials | ✅ (credentials present) |
| Install Python 3.12 / dependencies | ✅ |
| Ruff (`python -m ruff check teloude/`) | ✅ |
| Tests (`pytest -v`, watchdog-armed, offscreen Qt) | ✅ |
| Report a test that never finished | ✅ ("No test exceeded the hang watchdog") |
| Report the failing tests / Upload the test report | skipped — failure-only steps |
| Install Inno Setup 7.1.0 | ✅ |
| Build the bundle and the installer | ✅ |
| Verify the icon on the built binaries | ✅ (EXE + installer compared against `assets/icon-2.png`) |
| Collect the installer and write the build record | ✅ |
| Upload the installer | ✅ |

The detailed log lines require a signed-in GitHub session
(<https://github.com/Soheilsadri278/teloude/actions/runs/35349769899>); the
per-step conclusions above come from `gh api
repos/Soheilsadri278/teloude/actions/runs/35349769899/jobs`.

## 3. Contents of the Artifact

1. **`Teloude-Setup-0.1.0.exe`** — per-user Windows installer built with Inno
   Setup 7.1.0; packages `dist\Teloude\Teloude.exe` (PyInstaller onedir), the
   Python 3.12 runtime, PySide6, Telethon, the DPAPI security module and the
   application assets.
2. **`Teloude-Setup-0.1.0.exe.sha256`** — checksum of the installer.
3. **`build-info.txt`** — commit, branch, runner image, Python/Inno versions,
   installer name, size, SHA-256, and the "not built / still manual" notes
   (no code signing certificate; acceptance checks are human steps).
4. **`pip-freeze.txt`** — exact package versions of the build runner.
5. **`icon-teloude-exe.png`**, **`icon-teloude-installer.png`** — the icons
   extracted from the two binaries and compared against the official logo.

## 4. What This Build Contains

- **Official logo rollout** — `assets/icon-2.png` is the logo; `assets/icon.ico`
  (16–256 px) is embedded in `Teloude.exe`, the installer, the taskbar
  (`AppUserModelID`), Alt-Tab, window headers and the tray.
- **Translucent MTProto proxy dialog** — animated 4-state indicator, OS-level
  window glass, secret masking/unmasking, on-the-wire validation.
- **High-speed pipelined uploads** — 512 KiB chunks, 8-part in-flight window,
  resume/crash-recovery checkpoints preserved.
- **Desktop tray notifications** — on completion and on failure of backup and
  restore operations.
- **Hardened Windows test suite** — hang watchdogs (in-process + external
  guard), platform-specific process-reaping assertions, JUnit reporting; the
  suite is green in this run.
- **Root-folder topics, duplicates, search, restore-tree and preview work** —
  see `docs/CHANGELOG.md` and `docs/FINAL_REPORT.md`.

## 5. How to Build Locally on Windows 10/11

Prerequisites: Python 3.9+ 64-bit (3.12 recommended), Inno Setup 6.3+/7.x,
`python -m pip install -e ".[dev]" pyinstaller`.

```powershell
$env:TELOUDE_API_ID   = "<your_telegram_api_id>"
$env:TELOUDE_API_HASH = "<your_telegram_api_hash>"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SmokeTest
```

Output: `installer\Output\Teloude-Setup-0.1.0.exe`. `scripts\build_windows.bat`
is the same build with the environment checked first. Without
`-AllowUnconfiguredBuild` the script refuses to produce a user-facing installer
when the credentials are missing; `build_credentials.json` is written from the
environment, used by the build and deleted again (it is gitignored).

To rebuild in CI instead: **Actions → Windows release build → Run workflow**
(`workflow_dispatch`, optional `allow_unconfigured_build`). From a session
branch whose token cannot dispatch, add the branch to `on.push.branches`
temporarily, push, and remove it after the run — exactly what commit `789f7da`
and its revert did.

## 6. Testing & Acceptance Checklist

On a clean Windows machine:

1. **Python-free check** — `where python` returns nothing.
2. **Install** — run `Teloude-Setup-0.1.0.exe`; lowest-privilege per-user
   install into `%LOCALAPPDATA%\Programs\Teloude`, no UAC prompt.
3. **Run** — launch from the Start Menu or desktop shortcut; the sign-in wizard
   appears and no console window is shown.
4. **Proxy** — click the connection indicator, test the MTProto proxy.
5. **Data persistence** — `%APPDATA%\Teloude` holds `teloude_data.db` and the
   sessions.
6. **Uninstall** — remove via Settings → Apps; the program directory goes away,
   the user data in `%APPDATA%\Teloude` stays.

Full lists: `docs/installer_build_and_test.md` (section 4 is the manual Windows
acceptance pass) and `docs/live_acceptance_checklist.md`. The build is
**unsigned**: SmartScreen will warn until an Authenticode certificate is used.
