# Two host workflows

Resolve `runtime/OpenComputer/OpenComputerCLI.exe` from the skill directory. All
examples use PowerShell argument arrays; identity values come from `headless
windows`. Replace sample values with the current authorized target. Never use a
fixed example PID or HWND as a real target.

## Host with persistent stdin/stdout sessions

The agent host must expose a process handle/session with persistent stdin and
stdout, or a task-local MCP launcher. Start the console process with the exact
binding and input grant, retaining that process handle. Send newline-delimited
MCP JSON-RPC on stdin and read responses from that process's stdout. Use normal
MCP initialization, an initialized notification, then tools/list and tools/call.
Do not spawn another process for each tool call: observations and authority
belong to the original process.

An initial request can use:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"open-computer-skill-host","version":"1"}}}
```

Follow with `notifications/initialized`; then call `observe` and retain its
receipt. The host's shell/session tool must preserve full UTF-8 JSON lines and
must not treat terminal echo as an MCP response. If its process facility closes
stdin immediately or only supports completed commands, it cannot use this mode.
Stop the original process/session when done; do not enable automatic respawn or
persist an input-authorizing command in global client configuration.

## Shell-only host using an already-running local model

Use `headless run` with the same binding. This route uses the supplied model
endpoint to plan desktop actions; the host's hosted model does not receive MCP
tools. It needs no Python or Node installation. Do not download, load, restart,
or select a different model as an implicit setup step.

```powershell
$cli = '<skill>/runtime/OpenComputer/OpenComputerCLI.exe'
& $cli headless windows
# Fill the identity and model values from discovery and the user's setup.
$runArgs = @('headless', 'run', '--window-id', '<HWND>', '--pid', '<PID>',
  '--process-start', '<FILETIME>', '--allow-input', '--ttl', '600',
  '--max-actions', '64', '--endpoint', '<existing-loopback-v1-endpoint>',
  '--model', '<already-loaded-model>', '--task', '<authorized-user-task>',
  '--observation-mode', 'auto', '--max-steps', '8', '--reasoning', 'auto')
& $cli @runArgs
```

The reasoning setting `auto` is the portable choice. Explicit `off` requests a
provider/template extension and is suitable only for a compatible server.
Neither setting starts or changes the model runtime. Semantic observations can
work with text models when controls contain enough information. Image fallback
needs vision support.

The final JSON distinguishes runtime completion from verified task success.
`task_completion_verified:false` requires checking the actual application state;
do not turn a model's final statement into proof. The process stops/disarms its
controller before exit. Stop, Ctrl+Alt+Esc, expiry and errors still end the grant.
If no compatible model is already available and the host cannot maintain stdio,
report the missing host capability instead of claiming universal plug-and-play.
