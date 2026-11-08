# Changelog

## 0.1.3 — 2026-11-07

- Add an optional read-only Rust helper for native capture and bounded Windows
  UI Automation observations, with password redaction and an owned-child timeout.
- Expose compact semantic observations through MCP and the local model runner.
  Keep image mode as the default; semantic-only mode never sends images, while
  automatic mode can request an image when controls are insufficient.
- Bind semantic action receipts to both original pixels and control text, and
  revalidate both in the Python controller before input.
- Add an explicit native-helper option, status reporting, and a separate semantic
  launcher when a prebuilt helper is included. No helper or model starts during
  installation.
- Add an opt-in image-versus-semantic benchmark reporting actual model counters
  when available. Offline request-size reductions are not token or latency claims.

## 0.1.2 — 2026-11-07

- Reuse PNG encoding for an exactly matching fresh capture, with a bounded
  single-frame cache. Every capture still reads the window and validates its
  identity and geometry.
- Give the model compact observation metadata and tool descriptions. Keep the
  task and recent complete action/result pairs instead of accumulating old
  screenshots and observation metadata.
- Add a configurable image size budget to the example runner. Convert model
  coordinates back to native pixels in code; retain the original full-resolution
  image for the existing action checks.
- Record model-reported token usage and separate observation, model, and action
  timing. Add an opt-in comparison benchmark that never dispatches tool calls.
- Reduce each observation to one authoritative local request while retaining
  controller identity, arming, cancellation, and fresh-image checks.
- Add an explicit `--reasoning off` option for servers supporting the compatible
  chat-template extension. The default leaves server reasoning settings unchanged.

## 0.1.1 — 2026-11-07

- Add a **Copy MCP config** button with instructions for connecting an agent app.
  Configuration uses the console Python interpreter, including when the panel
  was launched through `pythonw.exe`.
- Fix same-process window inspection that could deadlock a desktop test while
  the UI thread and HTTP worker waited on each other.
- Improve emergency-stop handling, navigation keys, emoji input, and cancelled
  drags. Reject malformed Unicode before sending any text.
- Require well-formed, single tool calls in the example model runner. Pin control
  authority, retain only the latest screenshot, and interrupt cancelled model
  requests promptly.
- Preserve the short observation lifetime for desktop input. A slow model's
  action can receive a fresh receipt only when a new screenshot and target
  identity exactly match its original observation.
- Wait for three matching captures before planning, so queued typing has time
  to appear. Reject windows that keep changing instead of acting on a transient
  frame. Pin controller instance identity to detect restarts as well as rearming.
- Add a disposable live-model test that checks actual typed text and button
  clicks and saves separate model, native-input, and final-state evidence.
- Harden MCP request validation and make HTTP concurrency tests deterministic.

## 0.1.0 — 2026-11-07

- Initial Windows preview: local desktop panel, visible control overlay,
  screenshots and input tools, authenticated local service, and MCP bridge.
- Add a source installer, connection configuration helper, diagnostics,
  independent computer-use skill, and Windows CI.
