; installer/teloude.iss — Inno Setup 6 script for Teloude (PROJECT_SPEC phase 9,
; sections 3 and 11). Build the application first, then compile this script:
;
;   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
;
; ...or by hand, after `python -m PyInstaller teloude.spec`:
;
;   iscc installer\teloude.iss
;
; Output: installer\Output\Teloude-Setup-<version>.exe
;
; What a user gets: a per-user install (no administrator prompt), a Start Menu
; entry, an optional desktop shortcut, an optional "start with Windows" entry
; that launches minimized to the tray, a normal Add/Remove Programs entry, and
; an uninstaller that never touches the user's data or Telegram session.
#define MyAppName "Teloude"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Teloude"
#define MyAppExeName "Teloude.exe"

[Setup]
; A stable AppId is what lets a newer installer upgrade an existing install and
; makes Windows treat both as the same product (spec phase 9: upgrade handling).
AppId={{8F3C1D42-2B7E-4C5A-9E10-6A2F5D7C4B31}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Spec section 3: Windows 10/11, 64-bit.
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install: no UAC prompt, and %LOCALAPPDATA%\Programs\Teloude.
PrivilegesRequired=lowest
; Upgrades: close the running app instead of failing, and keep the previous
; directory so an upgrade lands in the same place.
CloseApplications=yes
UsePreviousAppDir=yes
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=Output
OutputBaseFilename=Teloude-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
; User data (SQLite index, logs, previews, DPAPI-protected Telegram session)
; lives in %APPDATA%\Teloude, outside {app}, and is deliberately never removed:
; uninstalling must not destroy the local index that describes the user's
; Telegram backups, and must never imply cloud cleanup (spec sections 11, 20).

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "autostart"; Description: "Start Teloude with Windows (minimized to tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller bundle: the application, the Python runtime and every
; dependency, so the installed program needs no Python, pip or compiler.
Source: "..\dist\Teloude\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Autostart launches minimized to the tray (--minimized). Uninstalling removes
; this value because the installer created it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the program directory itself (leftovers from an older PyInstaller
; version, for example). Deliberately no entry for {userappdata}\Teloude or the
; session directory: uninstall keeps the database, the logs and the Telegram
; session so a reinstall finds everything again (spec sections 11 and 20).
Type: filesandordirs; Name: "{app}"
