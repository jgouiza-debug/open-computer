# Open Computer architecture

The Tk desktop panel owns a Controller and its native WindowsBackend. The user
selects a window and presses ARM locally. A passive blue border identifies the
target, and a labelled blue pointer marks each action. The global Ctrl+Alt+Esc
watcher calls stop independently of the Tk event loop.

The HTTP transport binds only to 127.0.0.1. Each launch generates a random bearer
token and writes its URL and token to the current user's
`~/.opencomputer/connection.json`. Windows inherits the user's profile ACL;
POSIX files use owner-only permissions. There is no browser UI, CORS permission,
remote arm, remote reset, shell tool, or arbitrary Python execution tool. Requests
with browser Origin headers or incorrect Host headers are rejected before tool
dispatch. Keep the connection file private; another program running as the same
user can already read it and is outside this boundary.

`get_window_state` captures the selected window's client area as PNG and creates one observation
receipt. Screenshot coordinates start at that client area's top-left screen position,
excluding the native title bar and resize frame. Each action consumes that receipt. The next action requires another
capture. Receipts bind window identity, geometry and age; the backend checks
foreground focus and visible pointer ownership before native input. Target
selection disarms input. Emergency stop cancels in-progress work, invalidates
receipts and requires local reset and arming. Physical modifier/button presses
cause rejection rather than changing the meaning of agent input.

The MCP stdio bridge uses newline-delimited JSON-RPC and proxies only the documented
tools to the running GUI service. Captures become MCP image content blocks with
observation metadata in a separate text block. Stdout contains protocol messages
only. Open-source agents can use the bridge without any proprietary desktop
automation service.

The optional model runner calls an already running local OpenAI-compatible
`/v1/chat/completions` endpoint. It supplies image observations and tool schemas,
accepts one action per model response, refreshes afterward, and stops after a
bounded number of steps. It cannot select or arm a target and always stops on
completion or failure. No model is downloaded, loaded, or restarted. Actual model
quality and provider compatibility require testing with a running vision/tool
model; fixture tests alone do not establish those properties.

Windows input is best-effort: Windows can reject focus changes or input to elevated
applications, protected surfaces may not capture, and secure desktop/UAC surfaces
are unsupported. Observations can become outdated between any check and native
input; the short checks and cancellation reduce that race, not eliminate it.
Screen content is untrusted data and cannot authorize actions on behalf of users.

`python scripts/native_smoke.py --run --wait-for-focus 60` creates its own disposable native fixture, verifies
its PID before arming, types known text, clicks its own button, rejects observation
reuse, and records PNGs plus a JSON receipt. This is native fixture proof, not
validation of arbitrary applications or a model-driven task. Click the temporary
fixture locally when prompted: Windows may refuse foreground activation from a
background launch. The optional focus wait never relaxes the backend's checks.
