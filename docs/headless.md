# Headless agent skill

The primary workflow is an installable Windows agent skill backed by a headless
console runtime. No Open Computer panel or wizard is required. The host acts on
the user's desktop task and may bring its target application into the foreground.

**v0.1.6 is still a candidate; its release assets are not published.** The
downloaded package from commit `469b26d` passed clean PowerShell 5 and PowerShell 7
installs under long/Unicode paths, frozen help/headless-help/offline doctor checks,
and a no-GUI portable smoke test. The latter discovered 13 tools and made
read-only calls against an isolated fake controller through the legacy MCP
bridge. It did not perform a real desktop task. This is evidence for that exact
candidate, not a validation of subsequent rebuilds or arbitrary applications.
The [candidate build](https://github.com/jgouiza-debug/open-computer/actions/runs/42487776432)
records its source and packaging run.

The published v0.1.5 portable archive does not contain the headless command. Its
optional panel workflow uses separate [setup instructions](portable.md) and MCP
configuration. Do not use its older assets to install this skill.

## Install without Python or Rust

The skill contains the complete portable runtime, its component licenses, and a
short workflow guide. Windows 10 or newer on x64 and PowerShell are required.
No global Python, Rust, Node, PATH modification, administrator access or model
installation is needed. The shared portable payload still contains the legacy
GUI files; the headless process does not start or import that panel.

The release build produces these candidate assets:

- `OpenComputer-<version>-windows-x64-skill.zip` and its SHA-256 sidecar.
- `Install-OpenComputerSkill.ps1` and its SHA-256 sidecar.
- `install-skill-command.txt`: one PowerShell command containing the exact
  installer and archive hashes for that release.

Once those assets are published and verified, the command downloads the
installer, verifies its pinned hash before execution, and installs the pinned
skill archive. It does not start a desktop task or register a global MCP server.
Do not replace its hashes with values from an unrelated release. The generated
candidate command's public download URLs are not usable before publication.

For an already extracted headless-capable portable folder, run its installer:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-OpenComputerSkill.ps1 -PortablePath "$PWD"
```

For an already downloaded skill archive, supply its expected published hash:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-OpenComputerSkill.ps1 -ArchivePath '<absolute-skill-zip-path>' -ExpectedArchiveSha256 '<published-sha256>'
```

The default destination is `CODEX_HOME/skills/open-computer`, or
`~/.codex/skills/open-computer` when `CODEX_HOME` is unset. Use `-SkillRoot` for
another host's absolute skills directory. The installer does not change that
host's configuration. It verifies the manifest inventory and runs only console
help and offline dependency checks. The receipt records versions and manifest
hashes; no target or model is contacted during installation. Read that UTF-8
receipt when checking a Unicode install location: redirected PowerShell console
output can use a legacy encoding and lose characters in its printed path.

An existing directory is preserved unless `-Upgrade` is explicit and the
installation is managed, unchanged, and inactive. Local edits and additional
files cause an error rather than being overwritten. Close this installation's
sessions first. A failed verification leaves the previous installation intact.

## Give one task a fixed target

The trusted agent host uses the user's request to choose a window. It may launch
the intended application and activate it normally. This is not background-only
input, and there is no Open Computer panel or additional mandatory confirmation.

```text
OpenComputerCLI.exe headless windows
OpenComputerCLI.exe headless serve --window-id HWND --pid PID --process-start FILETIME --allow-input --ttl 600
```

Use the actual HWND, PID and process-start value from discovery. A Windows app
launcher can have a different PID from its window. Input requires
`--process-start`; omitting `--allow-input` keeps the session read-only. TTL is
1..3600 seconds, default 600. `--max-actions` accepts 1..256, default 64.

The input grant is fixed at launch. MCP cannot change its target, arm, reset or
resume it. Stop, Ctrl+Alt+Esc, expiry, budget exhaustion, EOF and owner
cancellation end the session. A new authorized task needs a new host launch.
This confines accidental task drift; it is not an OS security boundary against
a malicious program running as the same user.

## Choose the host workflow

A host with task-local MCP or persistent stdin/stdout session tools can retain
one `headless serve` process and use ordinary MCP JSON-RPC. Its own model chooses
the tools. It must preserve UTF-8 protocol lines, keep stdin open and disable
automatic respawn after stop or exit. A static global MCP entry is not a
substitute for this task lifecycle. The installed skill includes concrete
initialization and lifecycle guidance in `references/hosts.md`.

For a shell-only host, `headless run` delegates the desktop task to an
already-running compatible local model. It accepts the same target binding plus
`--endpoint`, `--model` and `--task`. This is different from exposing MCP tools
to the host's own hosted model. It does not download, load or restart a model.
Use `--reasoning auto` for portable server behavior; explicit `off` requires a
compatible provider/template extension. Consult `headless run --help` for the
remaining bounded runner options.

Auto observation prefers compact accessibility text and uses images only for
expected unavailable/incomplete/empty semantic coverage. Image fallback needs
a vision-capable model. Native screenshots still protect action authority even
when only text reaches the model. Inspect actual results after actions: a
successful tool dispatch or model final message alone is not task-success proof.
The direct runner reports `task_completion_verified:false` for this reason.

No universal host, model or application compatibility is claimed. Both fake
installer boundary tests and actual packaged installation have passed for the
candidate identified above. Neither establishes real-application task accuracy
or model performance. That validation used no GUI, model, desktop input or
native-helper protocol calls and was not a fresh-Windows VM test. Keep model
load, task latency, token usage and success/failure evidence separate when
evaluating this workflow.
