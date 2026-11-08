# Windows portable preview

The portable build packages the desktop panel, MCP bridge, Python, Tk, and Pillow
in one folder for Windows 10 or newer, x64. You supply the model and model
client separately.

## Start the panel

1. Download the `OpenComputer-<version>-windows-x64-portable.zip` asset from the
   [releases page](https://github.com/jgouiza-debug/open-computer/releases).
2. Extract the entire archive to a writable folder. Keep both executables,
   `_internal`, and the other bundled files together.
3. Open `OpenComputer.exe`. Select a window and inspect the observation before
   using the local **Arm** control.

Ordinary startup does not enable the optional Rust helper. To use control-text
observations, start `Start-OpenComputer-Semantic.cmd` instead. This launcher can
briefly show a console window. It enables the bundled read-only helper but does
not select a target, arm control, or start a model.

**Stop** disarms control. **Ctrl+Alt+Esc** is the emergency-stop shortcut. After
an input timeout, observe the current window before deciding what to do next;
do not replay the input blindly.

## Connect a model client

Use **Copy MCP config** in the panel, or export a configuration from PowerShell
in the extracted folder:

```powershell
.\OpenComputerCLI.exe setup config --output open-computer.mcp.json
```

The configuration uses the absolute path to the sibling `OpenComputerCLI.exe`,
`args: ["mcp"]`, and an empty environment object. Import it using the
[Unsloth or LM Studio instructions](clients.md). Export does not modify a
client's settings. Regenerate the configuration after moving the folder.

The private connection file remains in your user profile at
`%USERPROFILE%\.opencomputer\connection.json`. It contains a panel token; do not
include it in bug reports. The exported MCP configuration contains no token.

## Check the installation

These commands print to the console without opening the panel:

```powershell
.\OpenComputerCLI.exe --help
.\OpenComputerCLI.exe setup doctor --offline
.\OpenComputerCLI.exe run --help
```

For another working directory, invoke the executable by its absolute path.
`doctor --offline` checks packaged dependencies without contacting the panel.
The `run` command accepts the same model-runner options as the source version;
the bundle does not download or launch a model server.

## Build and validation scope

The manual `Portable Windows candidate` GitHub Actions workflow builds from its
selected source commit with locked dependency hashes. The bundle includes
`build-manifest.json` and component licences. An archive SHA-256 sidecar is
published alongside the ZIP.
The manifest records the source, toolchain, file hashes, and validation limits.

For development, `scripts/portable_smoke.py --help` describes the opt-in
relocated-folder checks. Run it from a source checkout using a separate Python
environment with the project's dependencies and MCP SDK 1.26.0 installed. It
checks the frozen CLI against a fake controller; it does not run a model or
send desktop input. GUI startup requires the separate `--include-gui` option.

This is an unsigned preview. A relocated-folder test with a restricted `PATH`
does not prove operation on a fresh Windows VM. Consult the release notes for
the checks actually run against that particular archive. Reproducible file
selection and pinned inputs do not establish bit-for-bit reproducible binaries.

The separate Python source/wheel installation remains supported. The portable
launcher does not update that installation or change global Python, PATH, or
model settings.
