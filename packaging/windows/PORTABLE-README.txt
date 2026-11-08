Open Computer - Windows x64 portable preview

Requires Windows 10 or later (x64). The system-provided Universal CRT is used;
Windows 8.1 and earlier are not supported.

Extract the complete folder before starting. Keep both EXEs and _internal together.
Double-click OpenComputer.exe for the ordinary panel. No Python, pip, Rust,
administrator account, PATH change or background model service is required.
You supply and run any model separately. This package never starts a model.

Optional control-text observations:
The optional/ helper is read-only. Ordinary startup does not load or enable it.
Explicitly run Start-OpenComputer-Semantic.cmd to start the panel with that helper.
That launcher can briefly show a command window. It does not select or arm a target.
Alternatively, from PowerShell in this extracted folder:
  & .\OpenComputer.exe --native-helper "$PWD\optional\opencomputer-native.exe"

Select a window locally, inspect it, and use the local Arm control only when ready.
Stop disarms immediately. Ctrl+Alt+Esc is the emergency-stop shortcut.
Observe again before each action; never repeat an input blindly after a timeout.

Console commands (use an absolute executable path from other folders):
  .\OpenComputerCLI.exe --help
  .\OpenComputerCLI.exe setup doctor --offline
  .\OpenComputerCLI.exe setup config --output open-computer.mcp.json
  .\OpenComputerCLI.exe mcp
  .\OpenComputerCLI.exe run --help

For a model client, start the GUI, leave it disarmed, and copy its MCP configuration.
The configuration targets the sibling OpenComputerCLI.exe with args ["mcp"], env {}.
The private connection file stays in your user profile at
  %USERPROFILE%\.opencomputer\connection.json
It is not bundled and must not be sent with reports. A moved folder needs fresh
copied configuration because executable paths are absolute. An explicit --python
setup override is for the separate source-installed workflow, not this bundle.

The Python source/wheel installer remains supported. This is an unsigned preview;
Windows reputation prompts may appear. No advice to disable security software is
part of this release. A relocated/restricted-PATH test is not a fresh-Windows VM test.

Open Computer is MIT licensed; third-party terms remain applicable. Exact notices
and component provenance are in THIRD_PARTY_NOTICES and build-manifest.json.
CPython's distributed LICENSE.txt includes Microsoft Distributable Code restrictions.
Build-tool notices are separated from notices for shipped runtime components.

https://github.com/jgouiza-debug/open-computer
