# Security

Open Computer is experimental. It allows an agent to observe screenshots and send
desktop inputs after a human selects and arms a target in the local panel.

The service binds to IPv4 loopback, authenticates requests with a per-launch
token, and rejects browser-origin requests. Remote HTTP/MCP tools cannot arm or
reset emergency stop. The connection file contains the token: keep it private and
never include it in issues, screenshots, model prompts, or chat logs.

These controls are not a security sandbox. Windows shares input with other apps
and the user. A compromised process running as your account can access local
files or invoke Python code directly. Use a separate desktop or virtual machine
for untrusted agents, and choose model endpoints according to the sensitivity of
the screenshots they will receive. The project cannot prevent an authorized
agent from misinterpreting a screen or attempting an unwanted task.

To report a vulnerability, use this repository's **Security → Report a
vulnerability** option if private reporting is enabled. If that option is absent,
open an issue asking for a private reporting channel without including exploit
details, credentials, or private images. Do not publish a working exploit before
maintainers have had a chance to respond.

Only the current source version is maintained. No formal security audit or
response-time commitment is provided.
