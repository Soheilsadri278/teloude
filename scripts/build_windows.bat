@echo off
rem ---------------------------------------------------------------------------
rem  Build the Teloude Windows release: dist\Teloude\Teloude.exe and
rem  installer\Output\Teloude-Setup-<version>.exe
rem
rem  Usage (from the repository root, or double-click this file):
rem      scripts\build_windows.bat              - build with the credentials that
rem                                               are already in the environment
rem      scripts\build_windows.bat -SmokeTest   - also start the packaged app once
rem      scripts\build_windows.bat -AllowUnconfiguredBuild
rem                                             - internal test build without
rem                                               baked-in credentials
rem
rem  Extra arguments are passed straight through to build_windows.ps1.
rem ---------------------------------------------------------------------------
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" echo. & echo Build failed with exit code %EXITCODE%.
exit /b %EXITCODE%
