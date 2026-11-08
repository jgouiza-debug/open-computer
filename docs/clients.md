# Connect a local model client

See [compatibility evidence](mcp-compatibility.md) for the tested clients and
the distinction between protocol checks, desktop observations, and model tasks.

For the [portable Windows preview](portable.md), extract the whole folder and
open `OpenComputer.exe`. For a Python source installation, run `install.cmd`,
then `start-open-computer.cmd`. Keep the panel disarmed for the initial
connection check. To enable text observations, use the portable bundle's
`Start-OpenComputer-Semantic.cmd`, or the source preview's
`start-open-computer-semantic.cmd` when it includes the Rust helper.

## Generate the configuration

Click **Copy MCP config** in either panel. For a portable installation, you can
also run this from its extracted folder:

```powershell
.\OpenComputerCLI.exe setup config --output open-computer.mcp.json
```

For a Python source installation, use:

```powershell
.\.venv\Scripts\python.exe -m opencomputer.setup config --output open-computer.mcp.json
```

The file contains an `mcpServers` object with one `open-computer` entry. Its
absolute executable paths support folders with spaces and non-Latin characters.
Keep its `command`, `args`, and `env` together. The portable configuration targets
the sibling `OpenComputerCLI.exe` with `args: ["mcp"]` and `env: {}`; the source
configuration uses its own Python installation. It contains no panel token,
but its paths identify your local installation. Regenerate it on each computer
and after moving the installation folder.

Export creates a new UTF-8 file. It refuses an existing destination unless you
explicitly add `--force`, which replaces that file. Create the destination
directory first. Export does not edit or merge any client's settings. Without
`--output`, the command prints JSON to the terminal.

## LM Studio

LM Studio supports MCP servers starting with version 0.3.17. In its right sidebar,
open **Program**, then **Install > Edit mcp.json**. Add the generated
`"open-computer": { ... }` entry inside the existing `mcpServers` object,
preserving the other server entries, and save. If the file is empty, use the
entire generated JSON object. These menu names and configuration format follow
[LM Studio's official instructions](https://lmstudio.ai/docs/app/mcp).

Enable the server's tools in the conversation and ask the model to call
`status`. The response should show `armed: false`. A host tool-confirmation
dialog does not arm Open Computer; arming stays in the desktop panel.

The configuration follows LM Studio's documented format. A live LM Studio
conversation has not yet been validated for this release.

## Unsloth Studio

In the chat's MCP menu, open **Manage MCP servers**, then **Import config** and
choose the exported JSON file. `config --format unsloth` produces the same
`mcpServers` format. To enter the server manually, choose **Add server**:

- Set **Display name** to `open-computer`.
- In **URL or executable** (shown as **Executable** for a local command), enter
  only the generated `command` path, without shell quotes or appended arguments.
- Use **Add argument** for three separate rows: `-P`, `-m`, `opencomputer.mcp`.
- Under **Environment variables**, use **Add variable** to enter `PYTHONPATH`
  and the generated environment value.
- Click **Add server**, turn on **Enable server**, and use **Refresh tools**.

Enable MCP in the current chat and choose a model that supports tool calls.
These labels were checked in the installed Studio frontend; versions may differ.
Call `status` before selecting or arming a target.
Give the model the workflow in [SKILL.md](SKILL.md). The tested local Unsloth
installation discovered all 13 tools and returned a real semantic observation
containing nine Windows controls as one text block, with no image block. This
verified wiring and observation, not autonomous task completion.

Image observations require a vision model with tool calling. Unsloth's tested
image path resizes images to a maximum edge of 1024 pixels; follow the coordinate
scaling rules in the skill. Semantic observations can use a text model, but only
where the accessibility tree exposes the information the task needs. See
[semantic observation modes](semantic.md) for canvas and image fallback limits.

## First task and recovery

1. Call `status` while the panel is disarmed to verify the connection.
2. Select the intended window in the panel and request a fresh observation.
3. Arm the selected window locally when you are ready for input.
4. Observe, perform one action, and observe again. Disarm or press Stop to end
   input authorization; Ctrl+Alt+Esc is the emergency stop shortcut.

The standalone `opencomputer.model_client` runner requires a selected, armed
session before any task, including descriptions. Use the MCP observation tools
directly for read-only checks while disarmed.

The MCP bridge reads `~/.opencomputer/connection.json`; it contains the panel's
private token. Use the bridge's `--connection PATH` argument for another instance.
After a panel restart, call `status`, list tools, or request a fresh observation
to establish the new connection. A state-changing call made against changed
connection information fails with `connection_changed` and is not replayed.
Issue another action only after reading the current session and checking its
target and state. The new panel still requires local arming.

The bridge can initialize before the panel exists. Read-only calls will report
an unavailable connection until the panel starts. Refresh tools in the host if
its initial discovery failed. A missing or malformed connection file does not
terminate the bridge; fix the panel connection and explicitly read again.

If the bridge cannot start, verify that its configured executable still exists
and regenerate the configuration. For a portable bundle, keep the complete
extracted folder together and run `OpenComputerCLI.exe setup doctor --offline`.
For a source installation, rerun `install.cmd` if needed. Run
`.\.venv\Scripts\python.exe -m opencomputer.setup doctor` for a read-only check;
`doctor --offline` checks dependencies without contacting the panel.
