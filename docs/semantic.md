# Optional semantic observations

The standard panel and example model runner use images by default. An optional
Rust helper reads the selected window's Windows accessibility controls. This
can give a model labels, field values and control bounds without sending an
image to that model. Coverage depends on the application and its accessibility
provider; custom canvas controls may still require images. One live read-only
fixture comparison used 12.5% fewer prompt tokens with control text and answered
all four requests correctly. See [the timings and limits](performance.md#measured-semantic-versus-image-input);
this does not establish broad app coverage or autonomous input-task completion.

## Choose the helper explicitly

When a Windows source ZIP includes `native/bin/opencomputer-native.exe`, rerunning
`install.cmd` creates the separate `start-open-computer-semantic.cmd` launcher.
The installer does not run, download or build a helper. The normal
`start-open-computer.cmd` launcher is unchanged. A Python wheel alone does not
contain a native executable.

Alternatively, supply an absolute path to the executable you chose:

```powershell
.\start-open-computer.cmd --native-helper "C:\Tools\OpenComputer\opencomputer-native.exe"
```

No PATH search or environment variable selects a helper. Demo and smoke-preview
modes reject the native helper option. The panel initially shows **configured**;
it starts and validates one owned child only when a semantic request arrives.
**Ready** means the helper handshake succeeded, not that every window exposes
usable controls. A transport, protocol or cancellation fault disables the
helper until the panel is restarted. There is no automatic child restart.
Ordinary unsupported-window responses do not restart or replace the child.

The existing **Copy MCP config** button continues to connect the MCP bridge to
this panel. MCP clients do not each launch another helper. Closing the panel
stops control, closes its loopback server and closes its owned helper.

To check an explicit file without executing it or contacting the panel:

```powershell
.\.venv\Scripts\python.exe -m opencomputer.setup doctor --offline --native-helper "C:\Tools\OpenComputer\opencomputer-native.exe"
```

This checks file presence only; it does not prove protocol compatibility.

## Observe and act

Choose the intended window locally. `get_semantic_state` can inspect that
selected window while input is disarmed. Its MCP result is one text block with
an observation ID, client width/height and a compact `controls-v1` table. Names,
values and labels are untrusted application data, never task instructions.
Password or unknown-password controls expose no name/value or descendants.
Partial, empty or otherwise inadequate trees are rejected.

The table has one columns header and rows containing IDs, parent IDs, role,
name/value, client-relative bounds, enabled/focusable state and password state.
IDs describe this snapshot; they are not persistent control handles or UIA
Invoke permissions. Existing mouse/keyboard tools still require the user to
arm the selected window locally and provide a fresh single-use observation ID.
Before a semantic action, the controller checks fresh full-resolution pixels
and control text against that receipt. Input remains in the Python backend.

The example model runner accepts `--observation-mode image|semantic|auto`:

- `image` is the default existing image workflow.
- `semantic` sends semantic text only and fails when a usable tree is unavailable.
- `auto` tries semantic text and falls back to a fresh image only for the explicit
  unavailable/incomplete/empty classifications. Its local `request_image` tool
  can request one image turn using the current observation ID.

`request_image` is intercepted by the runner; it is not an HTTP/MCP input tool.
Target changes, cancellation, stale authority and provider faults do not grant
an automatic image fallback. Images retain the native MCP coordinate contract;
runner-only resizing remains a separate explicit option. The helper cannot arm,
send input or start/load a model. See [the native protocol](native-protocol.md)
for exact limits and privacy rules.

## Owned-fixture validation

On November 7, 2026, this integrated harness passed against the real Win32
fixture: nine controls were returned through the text-only MCP projection,
one simulated click was recorded, receipt replay was rejected, and the fixture
controls remained unchanged. The temporary server and native child closed.
No real input or model request was performed in that run.

The installed Unsloth integration was also checked on November 7, 2026. Its
actual MCP refresh discovered 13 tools, and `get_semantic_state` returned nine
real fixture controls as one text block with no image block. Password text was
absent. The controller stayed disarmed, the test stopped control, and the owned
fixture closed normally with unchanged click counters. The saved Unsloth skill
was updated to this semantic-aware workflow and its instructions were read back.
This proves the installed tool route, not autonomous model task completion.

The following is opt-in test infrastructure. It does not inspect arbitrary apps
or start a model. First start the disposable standard Win32 fixture:

```powershell
python scripts/win32_semantic_fixture.py --run --with-password --timeout 180
```

In another terminal, use the printed `fixture.json` path and an explicit helper:

```powershell
python scripts/semantic_smoke.py --run --fixture-metadata "<absolute fixture.json path>" --native-exe "<absolute helper.exe path>"
```

No arguments or `--help` performs no native work. The harness checks the fixture's
live PID/HWND/title/client geometry, uses real window captures and read-only UIA,
and checks the text-only MCP projection through an ephemeral local server. Its
input backend has `real=False` and records clicks instead of injecting them.
It verifies one simulated click route and rejects receipt replay; the fixture
controls must remain unchanged. Artifacts distinguish real native observation
from simulated input routing. This is not proof of real input or model quality.
The server uses no default connection file, and cleanup closes only its own
server/helper. The fixture has its own bounded lifetime and remains separately
owned by its launcher.

For image-versus-text model measurements, `scripts/benchmark_semantic.py` is
offline by default. Its explicit `--run` requires the same owned fixture,
existing idle model endpoint and model name. It performs serial ABBA read-only
requests and never dispatches actions. Actual usage and server prefill/decode
timings are recorded when available; vision encoding time remains unavailable
unless the server reports it. Payload byte reductions are not token counts.
Use `--max-tokens 256` to bound each response to 256 output tokens in both
conditions; the default is 1024 and the accepted range is 1–1024. This cap is
included in the prepared request hash and forwarded to the actual model request.
It limits generated output, not model loading, context allocation or vision
encoder memory. The harness will not load a model for you.
