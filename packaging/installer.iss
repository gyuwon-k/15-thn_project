#define MyAppName "PILOT Studio"
#define MyAppVersion "1.0.0"
#define MyAppExeName "PILOT-Studio.exe"
[Setup]
AppId={{6F6D0EE9-9DD2-443E-8A9F-E4854A7167BD}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=PILOT Project
DefaultDirName={localappdata}\Programs\PILOT Studio
DefaultGroupName=PILOT Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PILOT-Studio-Setup-1.0.0
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
MinVersion=10.0.17763
[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "바로가기:"; Flags: unchecked
[Files]
Source: "..\dist\PILOT-Studio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "downloads\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; DestDir: "{tmp}"; Flags: dontcopy
[Icons]
Name: "{group}\PILOT Studio"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\PILOT Studio"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "PILOT Studio 실행"; Flags: nowait postinstall skipifsilent
[Code]
function NeedsWebView2: Boolean;
var Version: String;
begin
  Result := not ((RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')));
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
var ExitCode: Integer;
begin
  Result := '';
  if not NeedsWebView2 then
  begin
    Log('Existing WebView2 Runtime verified. Component installation skipped.');
    Exit;
  end;
  ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    Result := 'WebView2 구성 요소를 실행할 수 없습니다. 정책과 설치 권한을 확인하세요.';
    Exit;
  end;
  Log('WebView2 installer exit code: ' + IntToStr(ExitCode));
  if ExitCode = 3010 then NeedsRestart := True;
  if NeedsWebView2 then
    Result := 'WebView2 런타임 설치를 확인할 수 없습니다. 종료 코드: ' + IntToStr(ExitCode) + '. 구성 요소를 복구한 뒤 다시 설치하세요.';
end;
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ForceDirectories(ExpandConstant('{localappdata}\PILOT Studio\data'));
end;
