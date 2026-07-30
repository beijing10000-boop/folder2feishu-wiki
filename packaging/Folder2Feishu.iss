#ifndef MyAppVersion
  #define MyAppVersion "1.0.0-rc.1"
#endif

#define MyAppName "Folder2Feishu Wiki"
#define MyAppPublisher "Folder2Feishu Wiki contributors"
#define MyAppExeName "Folder2Feishu.exe"

[Setup]
AppId={{9E0C94CD-86B2-4E9C-A41E-F92E8E028CA1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Folder2FeishuWiki
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=Folder2Feishu-Windows-x64-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
ChangesAssociations=no
SetupLogging=yes
MinVersion=10.0.17763

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked
Name: "autorun"; Description: "安装完成后启动迁移作业台"; GroupDescription: "启动选项："; Flags: checkedonce

[Files]
Source: "..\dist\Folder2Feishu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\迁移作业台"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 Folder2Feishu Wiki"; Filename: "{uninstallexe}"
Name: "{autodesktop}\迁移作业台"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动迁移作业台"; Flags: nowait postinstall skipifsilent; Tasks: autorun

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--remove-all-schedules"; Flags: runhidden waituntilterminated skipifdoesntexist
