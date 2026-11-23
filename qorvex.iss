; Qorvex installer - user-mode install, no admin rights needed.

#define MyAppName "Qorvex"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "PagalSarthak"
#define MyAppExeName "qorvex.exe"

[Setup]
AppId={{9F3C7B62-4A1E-4E8D-9C2F-QORVEXLOCAL}}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/PagalSarthak/qorvex
AppName={#MyAppName}
AppMutex=QorvexRunningMutex
DefaultDirName={localappdata}\{#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Qorvex-{#MyAppVersion}-setup
SetupIconFile=qorvex.ico
UninstallDisplayIcon={app}\qorvex.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional icons:"; Flags: checkedonce
Name: "startup"; Description: "Start Qorvex when I sign in to Windows"; \
  GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\qorvex.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "qorvex.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  IconFilename: "{app}\qorvex.ico"; Comment: "Qorvex - download with every connection"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  IconFilename: "{app}\qorvex.ico"; Tasks: desktopicon; \
  Comment: "Qorvex - download with every connection"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "Qorvex"; \
  ValueData: """{app}\{#MyAppExeName}"" --no-open"; Tasks: startup; \
  Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent
