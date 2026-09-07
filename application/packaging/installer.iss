; Inno Setup script for 健康生活服务平台.
; It installs application binaries only. The portable database is created by
; the app under {app}\data and is intentionally not registered as an installed
; file, so an ordinary uninstall does not deliberately delete user data.

#define MyAppName "健康生活服务平台"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "健康生活服务平台"
#define MyAppExeName "健康生活服务平台.exe"
#define MyAppId "{{8D35E158-4960-4A50-9D7D-66BDE27CC2FD}"
#define MyDistDir "..\dist\健康生活服务平台"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ollama_chat_app
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir=..\dist\installer
OutputBaseFilename=Ollama-Dual-Chat-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Excludes: "data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Deliberately no [UninstallDelete] section. Back up with the app's portable
; data export before uninstalling or upgrading; user-created data is not targeted.
