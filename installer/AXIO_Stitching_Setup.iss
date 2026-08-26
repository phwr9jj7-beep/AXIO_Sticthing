; AXIO_Stitching_Setup.iss - the fused Windows installer for AXIO Stitching Studio.
;
; Installs, from ONE setup executable:
;   * the desktop GUI              (AXIO_Stitching_Studio.exe, windowed)
;   * the MCP server + `axio` CLI  (AXIO_Stitching_MCP.exe, console - stdio-capable)
;   * the shared _internal payload (including the bundled agent skill sources)
; and, via the "agentsetup" task, wires the MCP server and the Agent Skill into every AI
; agent platform DETECTED on the machine: Claude Code (CLI + desktop app), ChatGPT desktop /
; Codex, Google Antigravity, Claude Desktop, and the Gemini CLI.
;
; DESIGN RULES (do not undo these when editing):
;   * The installer stays DUMB. All agent detection/registration is delegated to
;     `AXIO_Stitching_MCP.exe --cli agent install` - the same audited, hash-verified,
;     surgical mechanism the CLI exposes. One mechanism, no duplicated logic here.
;     `agent install` with no --target auto-detects and NEVER creates a config directory
;     for an app that is not installed.
;   * PrivilegesRequired=lowest: a per-user install with no UAC. Agent configs live in the
;     user profile, and an ELEVATED installer would wire the ADMIN's ~/.claude instead of
;     the user's - the classic bug this line prevents. {autopf} therefore resolves to
;     %LOCALAPPDATA%\Programs.
;   * [UninstallRun] runs `agent uninstall` BEFORE files are removed, so no platform config
;     is ever left pointing at a deleted executable. Its removal is hash-verified: anything
;     the user edited since install is kept and reported, never clobbered.
;
; Build:  scripts\build_installer.py  (runs PyInstaller, then ISCC with /DAppVersion=...)
; Manual: ISCC /DAppVersion=1.1.0 installer\AXIO_Stitching_Setup.iss

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define MyAppName "AXIO Stitching Studio"
#define MyAppPublisher "Ziyi Wong"
#define MyAppURL "https://github.com/phwr9jj7-beep/AXIO_Sticthing"
#define MyAppExeName "AXIO_Stitching_Studio.exe"
#define MyMcpExeName "AXIO_Stitching_MCP.exe"

[Setup]
; Keep this GUID stable across releases - it is the installer's identity for upgrades.
AppId={{8B1F2C6A-9D34-4E7B-A1C0-52A0511C7011}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AXIO_Stitching_Studio_{#AppVersion}_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "agentsetup"; Description: "Set up &AI agent integration (auto-detects Claude Code, ChatGPT/Codex, Google Antigravity, Claude Desktop, Gemini CLI - skips apps you don't have)"

[Files]
Source: "..\dist\AXIO_Stitching_Studio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Registers the MCP server + skill into every detected agent platform. Runs as the invoking
; user (no elevation anywhere in this installer), so the right profile is written. A failure
; here does not abort the install - the same command can be re-run any time:
;   "{app}\{#MyMcpExeName}" --cli agent install
Filename: "{app}\{#MyMcpExeName}"; Parameters: "--cli agent install"; \
    StatusMsg: "Registering AI agent integration (MCP server + skills)..."; \
    Tasks: agentsetup; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
; MUST run before file removal (Inno runs [UninstallRun] first): deregisters the MCP server
; and skill from every platform, hash-verified - user-edited entries are kept and reported.
Filename: "{app}\{#MyMcpExeName}"; Parameters: "--cli agent uninstall"; \
    RunOnceId: "AxioAgentUninstall"; Flags: runhidden
