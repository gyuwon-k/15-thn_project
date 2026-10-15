#ifndef MyAppVersion
  #error MyAppVersion must be supplied from release.json by build_release.py
#endif
#ifndef BuildId
  #error BuildId must be supplied from release.json by build_release.py
#endif
#ifndef PackageSource
  #error PackageSource must be supplied by the current verified build
#endif
#ifndef OutputRoot
  #error OutputRoot must be supplied by build_release.py
#endif

[Setup]
AppId={{6F6D0EE9-9DD2-443E-8A9F-E4854A7167BD}
AppName=PILOT Studio
AppVersion={#MyAppVersion}
AppPublisher=PILOT Project
DefaultDirName={localappdata}\Programs\PILOT Studio
DefaultGroupName=PILOT Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename=PILOT-Studio-Setup-{#MyAppVersion}
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\PILOT-Studio.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
MinVersion=10.0.17763

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; Flags: unchecked

[Files]
Source: "{#PackageSource}\PILOT-Studio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageSource}\releases\{#BuildId}\*"; DestDir: "{app}\releases\{#BuildId}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PILOT Studio"; Filename: "{app}\PILOT-Studio.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\PILOT Studio"; Filename: "{app}\PILOT-Studio.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\PILOT-Studio.exe"; Description: "PILOT Studio 실행"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\PILOT-Studio.exe')) then
    if not FileCopy(ExpandConstant('{app}\PILOT-Studio.exe'), ExpandConstant('{app}\PILOT-Studio.previous.exe'), False) then
      Result := '실행 중인 프로그램을 종료한 뒤 다시 설치해 주세요. 기존 런처를 보존할 수 없습니다.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ExitCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if not Exec(ExpandConstant('{app}\PILOT-Studio.exe'), '--activate {#BuildId}', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    begin
      if FileExists(ExpandConstant('{app}\PILOT-Studio.previous.exe')) then
        FileCopy(ExpandConstant('{app}\PILOT-Studio.previous.exe'), ExpandConstant('{app}\PILOT-Studio.exe'), False);
      RaiseException('새 버전 준비를 시작할 수 없습니다. 기존 활성 버전과 사용자 데이터는 보존됩니다.');
    end;
    if ExitCode <> 0 then
    begin
      if FileExists(ExpandConstant('{app}\PILOT-Studio.previous.exe')) then
        FileCopy(ExpandConstant('{app}\PILOT-Studio.previous.exe'), ExpandConstant('{app}\PILOT-Studio.exe'), False);
      RaiseException('새 버전 검증에 실패했습니다. 활성 버전을 변경하지 않았습니다. update-journal.json을 확인해 주세요.');
    end;
  end;
end;
