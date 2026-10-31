; AI Office Desktop 설치 파일 (Inno Setup 6).  관리자 권한 없이 내 계정에만 설치한다.
#define AppVer GetEnv("AIO_VERSION")
#if AppVer == ""
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{6C0F3B6E-8E0B-4E4C-9C55-3F1C2A7D9A10}
AppName=AI Office Desktop
AppVersion={#AppVer}
AppPublisher=AI Office (open source)
DefaultDirName={localappdata}\Programs\AIOfficeDesktop
DefaultGroupName=AI Office Desktop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AIOfficeDesktop-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\AIOfficeDesktop.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로 가기 만들기"; GroupDescription: "바로 가기:"

[Files]
Source: "dist\AIOfficeDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\AI Office Desktop"; Filename: "{app}\AIOfficeDesktop.exe"
Name: "{autodesktop}\AI Office Desktop"; Filename: "{app}\AIOfficeDesktop.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AIOfficeDesktop.exe"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent

; 지우기: 프로그램만 지운다. 설정·키(%APPDATA%\AIOfficeDesktop)와 자료 폴더(문서\AIOffice자소서)는 남긴다.
