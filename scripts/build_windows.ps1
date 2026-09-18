<#
.SYNOPSIS
    Builds the Teloude Windows release: the PyInstaller bundle and the Inno Setup installer.

.DESCRIPTION
    One command, no arguments required:

        powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

    or simply double-click scripts\build_windows.bat.

    The script:
      1. checks the build prerequisites (Python, PySide6, PyInstaller, Inno Setup 6),
      2. writes the Telegram API credentials from the environment into
         installer\build_credentials.json (gitignored) so the installed application
         starts for a user who has no environment variables to set (spec section 11),
      3. runs PyInstaller (teloude.spec)  ->  dist\Teloude\Teloude.exe,
      4. compiles the installer (installer\teloude.iss) -> installer\Output\Teloude-Setup-<version>.exe,
      5. deletes the generated credentials file again (empty, never committed),
      6. prints the exact path, size and SHA-256 of the installer.

    Nothing needs administrator rights, and nothing is pushed anywhere.

.PARAMETER ApiId
    Telegram api_id baked into the build. Defaults to $env:TELOUDE_API_ID.

.PARAMETER ApiHash
    Telegram api_hash baked into the build. Defaults to $env:TELOUDE_API_HASH.

.PARAMETER InnoSetupPath
    Full path to ISCC.exe when Inno Setup 6 is not in its default location.

.PARAMETER SkipPyInstaller
    Reuse the existing dist\Teloude bundle and only compile the installer.

.PARAMETER NoClean
    Skip "PyInstaller --clean" (keeps the cache, faster rebuilds).

.PARAMETER AllowUnconfiguredBuild
    Build without credentials. The result starts only when TELOUDE_API_ID and
    TELOUDE_API_HASH are set in the environment - useful for internal test builds,
    not for a release.

.PARAMETER SmokeTest
    After building, start dist\Teloude\Teloude.exe with --offline and a temporary
    data directory, then verify that it stayed up and logged no traceback.

.EXAMPLE
    $env:TELOUDE_API_ID = "123456"
    $env:TELOUDE_API_HASH = "0123456789abcdef0123456789abcdef"
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SmokeTest
#>
[CmdletBinding()]
param(
    [string]$ApiId = $env:TELOUDE_API_ID,
    [string]$ApiHash = $env:TELOUDE_API_HASH,
    [string]$InnoSetupPath,
    [switch]$SkipPyInstaller,
    [switch]$NoClean,
    [switch]$AllowUnconfiguredBuild,
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"

function Write-Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host ""; Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- repository ---
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
Set-Location $repoRoot
Write-Step "Repository"
Write-Ok $repoRoot

if (-not (Test-Path (Join-Path $repoRoot "teloude.spec"))) {
    Fail "teloude.spec not found - run this script from the repository (scripts\build_windows.ps1)."
}
if (-not (Test-Path (Join-Path $repoRoot "installer\teloude.iss"))) {
    Fail "installer\teloude.iss not found - this is not a complete checkout."
}

# ------------------------------------------------------------- prerequisites ---
Write-Step "Checking build prerequisites"

$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $version = & $cmd.Source -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) { $python = $cmd.Source; break }
        } catch { }
    }
}
if (-not $python) {
    Fail "Python 3.9+ (64-bit) is required to BUILD the release. End users never need it.`n       Install it from https://www.python.org/downloads/windows/"
}
Write-Ok "python: $python ($version)"

$pyVersion = & $python -c "import sys; print('%d%02d' % sys.version_info[:2])"
if ([int]$pyVersion -lt 309) { Fail "Python 3.9 or newer is required to build (found $version)." }

$arch = & $python -c "import struct; print(struct.calcsize('P') * 8)"
if ($arch -ne "64") { Fail "A 64-bit Python is required (spec section 3: Windows 10/11, 64-bit)." }

& $python -c "import PySide6" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "PySide6 is not installed for $python.`n       Run:  $python -m pip install -e "".[dev]""`n       On Python 3.14, PySide6 6.10.1 or newer is required (first release with 3.14 wheels)."
}

& $python -c "import telethon, pydantic, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "A runtime dependency is missing (telethon / pydantic / pillow).`n       Run:  $python -m pip install -e "".[dev]"""
}

if (-not $SkipPyInstaller) {
    & $python -m PyInstaller --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Fail "PyInstaller is not installed for $python.`n       Run:  $python -m pip install -e "".[dev]""   (or: $python -m pip install pyinstaller)"
    }
    Write-Ok "PyInstaller: $(& $python -m PyInstaller --version)"
}

$iscc = $null
if ($InnoSetupPath) {
    if (Test-Path $InnoSetupPath) { $iscc = (Resolve-Path $InnoSetupPath).Path }
    else { Fail "ISCC.exe not found at '$InnoSetupPath'." }
} else {
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
    foreach ($guess in @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if ($guess -and (Test-Path $guess)) { $iscc = $guess; break }
    }
}
if (-not $iscc) {
    Fail ("Inno Setup 6 is required to compile the installer (it is not needed to run Teloude).`n" +
          "       Install it from https://jrsoftware.org/isdl.php and run this script again,`n" +
          "       or pass the location:  -InnoSetupPath ""C:\Program Files (x86)\Inno Setup 6\ISCC.exe""")
}
Write-Ok "Inno Setup compiler: $iscc"

# ----------------------------------------------------------------- versions ---
function Get-DefinedValue($file, $name) {
    $match = Select-String -Path $file -Pattern ("^#define\s+" + $name + "\s+""([^""]*)""") | Select-Object -First 1
    if (-not $match) { return $null }
    return $match.Matches[0].Groups[1].Value
}

$versionLine = Select-String -Path (Join-Path $repoRoot "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
$projectVersion = if ($versionLine) { $versionLine.Matches[0].Groups[1].Value } else { $null }
$issVersion = Get-DefinedValue (Join-Path $repoRoot "installer\teloude.iss") "MyAppVersion"

Write-Step "Versions"
Write-Ok "pyproject.toml: $projectVersion"
Write-Ok "installer:      $issVersion"
if ($projectVersion -and $issVersion -and ($projectVersion -ne $issVersion)) {
    Write-Warn2 "pyproject.toml and installer\teloude.iss disagree; the installer name follows the .iss value."
}

$installerPath = Join-Path $repoRoot ("installer\Output\Teloude-Setup-" + $issVersion + ".exe")

# ------------------------------------------------------------- credentials ---
$credentialsFile = Join-Path $repoRoot "installer\build_credentials.json"
$credentialsWritten = $false

if ($ApiId -and $ApiHash) {
    Write-Step "Baking Telegram API credentials into the bundle"
    if ($ApiId -notmatch '^\d+$') { Fail "TELOUDE_API_ID must be a number (got a non-numeric value)." }
    if ($ApiHash -notmatch '^[0-9a-fA-F]{16,}$') { Fail "TELOUDE_API_HASH does not look like an api_hash." }
    $json = "{`n  `"api_id`": $([int]$ApiId),`n  `"api_hash`": `"$ApiHash`"`n}`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($credentialsFile, $json, $utf8NoBom)
    $credentialsWritten = $true
    Write-Ok "wrote installer\build_credentials.json (gitignored; removed again after the build)"
    Write-Ok "api_id value: $($ApiId.Length) digit(s), api_hash value: $($ApiHash.Length) character(s) - values are not printed"
} elseif ($AllowUnconfiguredBuild) {
    Write-Step "Credentials"
    Write-Warn2 "-AllowUnconfiguredBuild: the release will start only with TELOUDE_API_ID / TELOUDE_API_HASH set."
} else {
    Fail ("TELOUDE_API_ID and TELOUDE_API_HASH are not set, so the installed application could not`n" +
          "       start for a user without environment variables (spec section 11).`n`n" +
          "       Set them for this build:`n" +
          "         `$env:TELOUDE_API_ID  = ""<your api id>""`n" +
          "         `$env:TELOUDE_API_HASH = ""<your api hash>""`n`n" +
          "       Or, for an internal test build that requires them at runtime:`n" +
          "         scripts\build_windows.ps1 -AllowUnconfiguredBuild")
}

try {
    # ---------------------------------------------------------------- bundle ---
    if (-not $SkipPyInstaller) {
        Write-Step "Building the application bundle (PyInstaller)"
        $pyInstallerArgs = @("-m", "PyInstaller", "--noconfirm")
        if (-not $NoClean) { $pyInstallerArgs += "--clean" }
        $pyInstallerArgs += "teloude.spec"
        & $python @pyInstallerArgs
        if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed (exit $LASTEXITCODE)." }
    } else {
        Write-Step "Reusing the existing bundle"
    }

    $exePath = Join-Path $repoRoot "dist\Teloude\Teloude.exe"
    if (-not (Test-Path $exePath)) { Fail "dist\Teloude\Teloude.exe was not produced." }
    Write-Ok ("Teloude.exe: {0:N1} MB" -f ((Get-Item $exePath).Length / 1MB))

    $bundledCredentials = Join-Path $repoRoot "dist\Teloude\_internal\build_credentials.json"
    if ($credentialsWritten) {
        if (Test-Path $bundledCredentials) { Write-Ok "build credentials are inside the bundle" }
        else { Fail "the bundle does not contain build_credentials.json - startup would fail for end users." }
    }

    # ------------------------------------------------------------- installer ---
    Write-Step "Compiling the installer (Inno Setup)"
    & $iscc (Join-Path $repoRoot "installer\teloude.iss")
    if ($LASTEXITCODE -ne 0) { Fail "ISCC failed (exit $LASTEXITCODE)." }

    if (-not (Test-Path $installerPath)) { Fail "expected installer not found: $installerPath" }
    $installer = Get-Item $installerPath
    $hash = (Get-FileHash -Algorithm SHA256 -Path $installer.FullName).Hash.ToLower()

    # ------------------------------------------------------------ smoke test ---
    if ($SmokeTest) {
        Write-Step "Smoke test (offline, no account, temporary data directory)"
        $smokeDir = Join-Path $env:TEMP ("teloude-smoke-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $smokeDir | Out-Null
        $proc = Start-Process -FilePath $exePath `
            -ArgumentList @("--offline", "--data-dir", $smokeDir) -PassThru
        Start-Sleep -Seconds 20
        $alive = -not $proc.HasExited
        $log = Join-Path $smokeDir "logs\teloude.log"
        $traceback = $false
        if (Test-Path $log) { $traceback = (Select-String -Path $log -Pattern "Traceback" -Quiet) }
        if ($alive) { Stop-Process -Id $proc.Id -Force }
        if (-not $alive) { Fail "the packaged application exited during the smoke test (log: $log)." }
        if ($traceback) { Fail "the packaged application logged a traceback (log: $log)." }
        Write-Ok "app stayed up for 20s with no traceback (log: $log)"
        Write-Ok "remove the temporary data directory when done: $smokeDir"

        # The generic smoke test above only proves the application starts. The
        # proxy sheet failed *in the frozen build only* while every source test
        # was green, so the packaged binary is also exercised for that one flow.
        Write-Step "Proxy sheet smoke test (frozen EXE)"
        $proxyDir = Join-Path $env:TEMP ("teloude-proxy-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $proxyDir | Out-Null
        $proxyLog = Join-Path $proxyDir "startup_error.log"
        $env:TELOUDE_DIAG = "1"
        $env:TELOUDE_DIAG_LOG = $proxyLog
        # The application clicks its own connection icon and reports whether the
        # sheet was really drawn - no desktop automation needed.
        $selfTest = Start-Process -FilePath $exePath `
            -ArgumentList @("--offline", "--data-dir", $proxyDir, "--self-test-proxy") `
            -PassThru -Wait
        if (Test-Path $proxyLog) { Get-Content $proxyLog | ForEach-Object { Write-Host "      $_" } }
        if ($selfTest.ExitCode -ne 0) {
            Fail "the packaged application did not show the proxy sheet (exit $($selfTest.ExitCode); log: $proxyLog)."
        }
        Write-Ok "the packaged application opens the proxy sheet (log: $proxyLog)"
    }

    # ------------------------------------------------------------------ done ---
    Write-Step "Done"
    Write-Host ""
    Write-Host ("  Installer : " + $installer.FullName) -ForegroundColor White
    Write-Host ("  Size      : {0:N1} MB" -f ($installer.Length / 1MB))
    Write-Host ("  SHA-256   : " + $hash)
    Write-Host ("  Bundle    : " + $exePath)
    Write-Host ""
    Write-Host "  Test it the way a user will:" -ForegroundColor White
    Write-Host "    1. run the installer (no administrator prompt: it installs per user)"
    Write-Host "    2. launch Teloude from the Start Menu (no terminal, no Python)"
    Write-Host "    3. sign in with Telegram"
    Write-Host "    4. uninstall from Settings > Apps and check that"
    Write-Host "       $env:APPDATA\Teloude still contains the database and the session"
    Write-Host ""
} finally {
    if ($credentialsWritten -and (Test-Path $credentialsFile)) {
        Remove-Item -Force $credentialsFile
        Write-Ok "removed installer\build_credentials.json (the values live only inside the bundle)"
    }
}
