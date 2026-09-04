; installer/teloude.iss — Inno Setup script for Teloude (Phase 9 packaging).
; Build the app first with `pyinstaller teloude.spec`, then compile this with
; Inno Setup 6:  iscc installer\teloude.iss
; Output: installer/Output/Teloude-Setup-0.1.0.exe
#define MyAppName "Teloude"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Teloude"
#define MyAppExeName "Teloude.exe"

[Setup]
AppId={{3F2A1B7C-9D4E-4F2A-8C1D-TeloUde01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=Teloude-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; User data (SQLite index, logs, previews) lives in %APPDATA%\Teloude and is
; deliberately NOT removed on uninstall: uninstalling must never delete backups
; metadata the user's Telegram cloud files depend on (spec section 20).

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "autostart"; Description: "Start Teloude with Windows (minimized to tray)"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\Teloude\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Autostart launches minimized to the tray (--minimized flag).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent
