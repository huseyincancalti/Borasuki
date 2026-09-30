#define AppVersion "1.0.0-beta.1"
#ifndef BuildRoot
  #error BuildRoot must point to the verified PyInstaller Borasuki folder.
#endif
#ifndef WebView2Bootstrapper
  #error WebView2Bootstrapper must point to a verified Microsoft-signed bootstrapper.
#endif

[Setup]
AppId={{A8012663-A8E7-41FF-B108-768F7B238FB2}
AppName=Borasuki
AppVersion={#AppVersion}
AppPublisher=Hüseyin Can ÇALTI
AppPublisherURL=https://karakedidub.com
AppSupportURL=https://github.com/huseyincancalti/Borasuki/issues
DefaultDirName={localappdata}\Programs\Borasuki
DefaultGroupName=Borasuki
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Borasuki-Setup-{#AppVersion}-win64
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\frontend\assets\img\logo.ico
UninstallDisplayIcon={app}\Borasuki.exe
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#WebView2Bootstrapper}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\Borasuki"; Filename: "{app}\Borasuki.exe"
Name: "{autodesktop}\Borasuki"; Filename: "{app}\Borasuki.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2 Runtime…"; Flags: waituntilterminated; Check: not HasWebView2
Filename: "{app}\Borasuki.exe"; Description: "Launch Borasuki"; Flags: nowait postinstall skipifsilent

[Code]
function HasWebView2(): Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
            (RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;
