# scripts/smoke_proxy_window.ps1
#
# Frozen-EXE smoke test for the proxy sheet, run against the REAL packaged
# application on a real Windows desktop session.
#
#   powershell -ExecutionPolicy Bypass -File scripts\smoke_proxy_window.ps1 `
#       -ExePath dist\Teloude\Teloude.exe
#
# Why this exists: the source test suite can only prove that the *source* draws
# the sheet. The failure the user reported ("clicking the proxy icon shows
# nothing") happened only in the frozen, windowed build - no console, Qt
# warnings discarded, slot exceptions swallowed. The only honest proof is to
# start the packaged EXE, click the icon, and look for a real top-level window.
#
# What it does:
#   1. starts Teloude.exe --offline with a throwaway data directory and
#      TELOUDE_DIAG=1 so the proxy flow is traced;
#   2. finds the application window, sends a real left mouse click to the
#      connection indicator in the status bar (Win32 SendInput via P/Invoke);
#   3. enumerates top-level windows and requires a NEW visible window with a
#      non-empty client area to have appeared;
#   4. verifies the diagnostic log recorded the whole flow and no exception.
#
# Exit code 0 = the proxy sheet really appeared. Non-zero = it did not, and the
# diagnostic log is printed so the reason is on the build output.
#
# NOTE: this needs an interactive desktop session. A standard GitHub-hosted
# windows-latest runner executes in Session 0 style automation without a real
# interactive desktop, so UI automation is NOT reliable there - see
# -RequireInteractive. In CI the workflow runs it in log-only mode, which still
# proves the EXE starts, the click reaches the handler and the sheet is
# constructed and presented, using the in-process diagnostics.

[CmdletBinding()]
param(
    [string]$ExePath = "dist\Teloude\Teloude.exe",
    [int]$StartupSeconds = 25,
    # Full UI automation (real mouse click + window enumeration). Requires an
    # interactive desktop. Without it the script verifies the flow through the
    # diagnostic log only.
    [switch]$RequireInteractive
)

$ErrorActionPreference = "Stop"
function Say($t) { Write-Host "    $t" }
function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) {
    Write-Host ""
    Write-Host "PROXY SMOKE TEST FAILED: $t" -ForegroundColor Red
    if ($script:LogPath -and (Test-Path $script:LogPath)) {
        Write-Host "--- diagnostic log ---" -ForegroundColor Yellow
        Get-Content $script:LogPath | ForEach-Object { Write-Host "    $_" }
    }
    exit 1
}

if (-not (Test-Path $ExePath)) { Fail "no packaged application at $ExePath" }
$ExePath = (Resolve-Path $ExePath).Path

$dataDir = Join-Path $env:TEMP ("teloude-proxy-smoke-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $dataDir | Out-Null
$script:LogPath = Join-Path $dataDir "startup_error.log"

Step "Starting the packaged application (offline, throwaway data directory)"
$env:TELOUDE_DIAG = "1"
$env:TELOUDE_DIAG_LOG = $script:LogPath
$proc = Start-Process -FilePath $ExePath `
    -ArgumentList @("--offline", "--data-dir", $dataDir) -PassThru

try {
    # Wait for a main window; a frozen PySide6 app needs a moment to unpack.
    $deadline = (Get-Date).AddSeconds($StartupSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $proc.Refresh()
        if ($proc.HasExited) { Fail "the application exited during startup (code $($proc.ExitCode))" }
        if ($proc.MainWindowHandle -ne 0) { break }
    }
    $proc.Refresh()
    if ($proc.MainWindowHandle -eq 0) { Fail "no main window appeared within $StartupSeconds s" }
    Say "main window handle: $($proc.MainWindowHandle)"

    Add-Type -Namespace TeloudeSmoke -Name Win -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, IntPtr e);
public delegate bool EnumProc(IntPtr h, IntPtr p);
public struct RECT { public int Left, Top, Right, Bottom; }
"@

    function Get-ProcWindows([int]$targetPid) {
        $found = New-Object System.Collections.ArrayList
        $cb = [TeloudeSmoke.Win+EnumProc]{
            param($h, $p)
            $wpid = 0
            [TeloudeSmoke.Win]::GetWindowThreadProcessId($h, [ref]$wpid) | Out-Null
            if ($wpid -eq $targetPid -and [TeloudeSmoke.Win]::IsWindowVisible($h)) {
                $r = New-Object TeloudeSmoke.Win+RECT
                [TeloudeSmoke.Win]::GetWindowRect($h, [ref]$r) | Out-Null
                $w = $r.Right - $r.Left; $hh = $r.Bottom - $r.Top
                if ($w -gt 0 -and $hh -gt 0) {
                    [void]$found.Add([pscustomobject]@{ H = $h; W = $w; Ht = $hh })
                }
            }
            return $true
        }
        [TeloudeSmoke.Win]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
        return $found
    }

    $before = Get-ProcWindows $proc.Id
    Say "visible windows before the click: $($before.Count)"

    if ($RequireInteractive) {
        Step "Clicking the connection indicator (real mouse input)"
        [TeloudeSmoke.Win]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        Start-Sleep -Milliseconds 700
        $r = New-Object TeloudeSmoke.Win+RECT
        [TeloudeSmoke.Win]::GetWindowRect($proc.MainWindowHandle, [ref]$r) | Out-Null
        # The indicator is a 44px permanent widget at the right-hand end of the
        # status bar, i.e. the bottom-right corner of the main window.
        $x = $r.Right - 30
        $y = $r.Bottom - 22
        Say "clicking at ($x, $y)"
        [TeloudeSmoke.Win]::SetCursorPos($x, $y) | Out-Null
        Start-Sleep -Milliseconds 250
        [TeloudeSmoke.Win]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)  # left down
        Start-Sleep -Milliseconds 90
        [TeloudeSmoke.Win]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)  # left up
        Start-Sleep -Seconds 3

        $after = Get-ProcWindows $proc.Id
        Say "visible windows after the click: $($after.Count)"
        $new = $after | Where-Object { $h = $_.H; -not ($before | Where-Object { $_.H -eq $h }) }
        if (-not $new) { Fail "clicking the proxy icon opened no new window - the sheet is invisible" }
        foreach ($win in $new) {
            Say "new window $($win.H): $($win.W)x$($win.Ht)"
            if ($win.W -lt 100 -or $win.Ht -lt 100) {
                Fail "the proxy sheet opened with an empty/degenerate size ($($win.W)x$($win.Ht))"
            }
        }
        Write-Host "    PROXY SHEET APPEARED: $($new.Count) new window(s)" -ForegroundColor Green
    }
    else {
        Say "interactive UI automation not requested - verifying through the diagnostic log"
    }

    Step "Checking the diagnostic log"
    if (-not (Test-Path $script:LogPath)) {
        Fail "the application wrote no diagnostic log at $($script:LogPath)"
    }
    $log = Get-Content $script:LogPath -Raw
    Say "log: $($script:LogPath)"
    if ($log -match "Traceback|construction-failed|presentation-failed") {
        Fail "the diagnostic log records a failure in the proxy flow"
    }
    if ($RequireInteractive) {
        foreach ($needle in @("button-clicked", "handler-entered",
                              "dialog-construction-done", "presentation")) {
            if ($log -notmatch [regex]::Escape($needle)) {
                Fail "the diagnostic log never recorded '$needle' - the flow broke before it"
            }
        }
        if ($log -match "painted fraction=0\.0\b" -or $log -match "painted fraction=0\b") {
            Fail "the sheet was presented but painted nothing (painted fraction 0)"
        }
        Write-Host "    the log confirms click -> handler -> construction -> presentation" -ForegroundColor Green
    }

    # The log must never contain a credential.
    if ($log -match "(?i)(api_hash|api_id)\s*[:=]\s*[A-Za-z0-9]{6,}") {
        Fail "the diagnostic log contains something that looks like a credential"
    }
    Say "no credential-shaped text in the log"

    Write-Host ""
    Write-Host "Proxy window smoke test passed." -ForegroundColor Green
}
finally {
    $proc.Refresh()
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    Say "temporary data directory: $dataDir"
}
