# MCP compatibility evidence

This page separates protocol tests from actual desktop and model tasks.

| Client or interface | Verified scope | Remaining limits |
| --- | --- | --- |
| Python MCP SDK 1.26.0 | A real stdio client negotiated protocol `2025-11-25`, discovered all 13 tools, read status, and received a structured tool error from v0.1.4. | The HTTP service used a disposable fake controller. This does not prove desktop input or model task completion. |
| Installed Unsloth Studio | Registration and refresh discovered 13 tools. A real Windows UI Automation observation returned nine controls as one text block, without an image block. The skill was installed and read back. | That v0.1.3 integration check made no model or input calls. Autonomous Unsloth task completion remains unverified. |
| LM Studio | Configuration format and setup steps were checked against its official documentation. | No live LM Studio conversation has been tested. |

The SDK check used source commit
`d9b73b6ba20d0e4c419b2568b1b0922b21bf86e2`. It launched a separate bridge process
and an authenticated HTTP service on a dynamically assigned loopback port. The
fake controller had no native backend, and every state-changing call was set to
fail the test. No model, real panel, screenshot, or input calls occurred.

## Timeouts and cancellation

The stdio bridge processes requests sequentially. A client's MCP cancellation
notification does **not** interrupt an HTTP request already running. Later
requests, including ping, wait for it to finish.

In the SDK probe, a fake status request deliberately took 1.25 seconds. The
client timed out after 212 ms; ping completed about 1,239 ms after the cancellation
notification, after the HTTP request finished. Baseline protocol checks passed,
but this result does not demonstrate cancellation support or low latency under
a stalled service.

Treat a tool timeout as an uncertain result. Do not automatically repeat an
input action. Use the panel's **Stop** control or **Ctrl+Alt+Esc** to stop local
input authorization, then obtain a fresh observation before deciding what to do
next. A client timeout is not an emergency stop.

For configuration and first-run checks, see [client setup](clients.md).
