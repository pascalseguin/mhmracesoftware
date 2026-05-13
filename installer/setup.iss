; installer\setup.iss
; Inno Setup script for MHM Race Management
;
; Prerequisites:
;   - dist\MHM-Race.exe must already be built (run build.ps1 first)
;   - Inno Setup 6 must be installed: https://jrsoftware.org/isdl.php
;
; To build the installer:
;   Open this file in Inno Setup IDE and press F9
;   OR from command line:
;   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
;
; Output: installer\output\MHM-Race-Setup.exe

[Setup]
AppName=MHM Race Management
AppVersion=2025.1
AppPublisher=Southeastern Alberta Search and Rescue Association
AppPublisherURL=https://mhmassacre.ca
AppSupportURL=https://mhmassacre.ca
AppUpdatesURL=https://mhmassacre.ca

; Install into the current user AppData\Local folder -- fully writable
; without administrator rights. The app writes its database and config
; next to the .exe, so it must NOT go into Program Files.
DefaultDirName={localappdata}\MHM Race
DefaultGroupName=MHM Race
DisableProgramGroupPage=yes

; No admin required -- the app is single-user on a race laptop
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Installer output
OutputDir=output
OutputBaseFilename=MHM-Race-Setup
SetupIconFile=

; Compression
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Appearance
WizardStyle=modern
WizardResizable=no

; Minimum OS: Windows 10
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; The single bundled executable -- everything is inside it
Source: "..\dist\MHM-Race.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Desktop shortcut
Name: "{userdesktop}\MHM Race"; Filename: "{app}\MHM-Race.exe"; \
  Comment: "MHM Race Management System"

; Start Menu shortcut
Name: "{userprograms}\MHM Race\MHM Race Management"; Filename: "{app}\MHM-Race.exe"
Name: "{userprograms}\MHM Race\Uninstall MHM Race";  Filename: "{uninstallexe}"

[Run]
; Offer to launch immediately after install
Filename: "{app}\MHM-Race.exe"; \
  Description: "Launch MHM Race Management now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove data files created by the app on uninstall (optional -- comment out to keep DB)
; Type: filesandordirs; Name: "{app}"

[Messages]
WelcomeLabel2=This will install MHM Race Management on your computer.%n%nMHM Race is the chip-timing and results software for the Medicine Hat Massacre orienteering event.%n%nClick Next to continue.

FinishedLabel=MHM Race Management has been installed.%n%nA shortcut has been placed on your Desktop. Double-click it to start the app, then open http://localhost:8080 in your browser.%n%nDefault login: admin / admin

[Code]
// Warn if an older copy of MHM-Race.exe is already running
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if CheckForMutexes('MHM-Race') then begin
    MsgBox('MHM Race appears to be running. Please close it before installing.', mbError, MB_OK);
    Result := False;
  end;
end;
