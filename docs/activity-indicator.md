# Activity indicator

Open Computer uses a headless skill with a passive blue outline around the task's
Windows application. There is no separate control panel or setup wizard in this
workflow. The outline identifies the session's target; it is not a claim that a
model action succeeded.

## The interaction pattern

ChatGPT's public documentation describes reading an allowed app, clicking or
typing, and inspecting the result. The user can interrupt or take control.
See [OpenAI's computer and browser walkthrough](https://developers.openai.com/training/walkthroughs/using-your-computer-and-browser).
This project implements an independent version of that interaction. It does not
claim to reproduce ChatGPT's private renderer or exact visual styling.

The visual design is a thin blue window outline using the project's existing
`#438fff` color. It follows the target while a session is active, including while
the model is thinking. It never takes keyboard focus or intercepts mouse input.
The first implementation is the outline; an animated AI cursor is separate work.

| State | Feedback |
| --- | --- |
| Discovering available windows | No outline |
| Bound read-only or input session | Blue outline on the selected window |
| Model planning | Outline stays visible; no animated fake activity |
| Stopped, expired, closed, or target lost | Outline disappears |
| Indicator cannot operate safely | Control ends with an error |

Press **Ctrl+Alt+Esc** to stop. Closing the host session also ends the grant.
Input cannot start until the indicator has acknowledged its initial visible
frame. Stopping is terminal: queued geometry updates cannot bring the outline
back after the session ends.

## Cost and capture behavior

The renderer uses Windows APIs on a local worker thread. Moving or drawing the
outline does not call the model and adds no model tokens. Geometry updates are
coalesced, and unchanged frames do not need repainting. This does not reduce model
inference latency; those measurements are reported separately.

Feedback must not change the app images used for model observations or freshness
checks. Normal borders are outside the client rectangle. Any edge fallback needs
verified capture exclusion and a native capture comparison. Native verification
must also check focus preservation, click-through styles, movement, stop, and
window cleanup; source or mock tests alone are not evidence of those behaviors.

## Verified on Windows

The 2026-11-08 source-runtime check passed seven disposable Win32 fixture cases:
normal, read-only, screen edge, monitor-fill geometry, minimize, expiry, and an
injected feedback fault. Foreground focus stayed unchanged, target images were
pixel-identical with the outline present, and fixture/frame cleanup passed.
Initial frame startup ranged from 16.096 to 18.740 ms in this run. There were no
model requests, input actions, or Tk imports. The unit suite passed 281 tests,
with one environment skip.

See the [sanitized validation receipt](outline-validation.json), including the
tested source hashes. This is native source-runtime evidence, not a packaged
runtime check or a claim about model task latency or arbitrary apps.
