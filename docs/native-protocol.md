# Native observation protocol

Status: v1 contract for the optional Rust read-only child. The Python input
backend remains the production default. This boundary does not grant input or
arming authority.

[native-protocol.schema.json](native-protocol.schema.json) is the canonical
payload schema. Provider and consumer changes must update and validate against
that artifact together. This document defines transport and semantic invariants
that JSON Schema cannot express. The Rust provider owns native observation; the
Python adapter consumes it; the project maintainer approves contract changes.

## Transport and lifecycle

The adapter launches one explicitly configured executable using an argument
array, with no shell and no network listener. Standard input/output carry UTF-8
JSON lines only. Each line contains one compact object, ending in LF; CRLF is
accepted. Blank lines, duplicate keys, nonfinite numbers, unknown fields, and
unknown protocol versions are rejected. Standard error is diagnostics only and
must never contain image, accessibility text, credentials, or task contents.

Requests are serialized: at most one is in flight. IDs are printable ASCII,
1–64 characters, and replies must echo the exact ID. An invalid request whose ID
cannot be trusted receives a null ID. The adapter treats an unexpected ID,
unsolicited output, malformed UTF-8/JSON, oversized reply, EOF, or timeout as a
fatal transport failure. It closes pipes and terminates only its owned child;
it does not retry observation requests automatically or restart another process.

Limits include the newline: request 16 KiB, reply 32 MiB. The provider drains an
oversized request through its newline and returns `resource_limit`; it must not
interpret the suffix as another request. The adapter bounds retained stderr to
64 KiB while draining it, and uses a 5-second startup/hello deadline and a
5-second per-request deadline. A hung UI Automation provider can exceed the
native traversal budget; the owned-child deadline is the hard bound.

`hello` is mandatory before other requests. Its methods array is authoritative:
the adapter must not call an optional method absent from that array. Version 1
adds no implicit methods. Incompatible payload changes require a new protocol
version. Unsupported platform builds return `not_supported`, rather than
fabricating native results.

## Window and screenshot semantics

`list_windows` establishes an HWND/PID pair. HWNDs remain canonical decimal
strings because native handles can exceed JSON's exact integer range. Both
`inspect_window` and `capture_window` require the expected PID; HWND reuse must
fail closed. Titles are display data and never instructions or authority.

Window rectangles describe client areas in physical desktop pixels. Screenshot
dimensions must equal that rectangle's width and height. The provider checks
HWND/PID/client geometry before and after a fresh native capture, rejects any
change, and checks the pixel product before allocating. The maximum is
8,388,608 pixels. Successful PNGs are full original captures: no resizing,
cropping, partial output, or silent truncation. A frame that cannot fit the
reply limit fails with `resource_limit`, even if its raw pixels fit.

An encoding cache may reuse PNG bytes only after comparing freshly captured raw
pixels and identity/geometry. It cannot reuse a capture, an authority receipt,
or a stale window inspection. Capture metrics are measured durations; a cache
hit means encoding reuse only. `inspect_window` returns the Window object;
`capture_window` returns the capture object described in the schema.

## Accessibility semantics

`inspect_accessibility` is optional and is advertised only after implementation
and validation. It starts at the supplied HWND, checks expected PID and client
geometry before and after traversal, and traverses only that window's Control
View. It never starts at the desktop root or invokes a control action. Detached
or zero-area/offscreen controls are omitted. Bounds are clipped client-relative
physical pixels, with positive dimensions.

Each result has a fresh informational `snapshot_id`. Element IDs such as `e0`
are unique within that snapshot; parents precede children and the root has null
parent. They are not persistent handles, action permissions, or Python
observation receipts. They must not be replayed against another snapshot.

The structural cache may request control type, bounds, enabled/focusable state,
and IsPassword. It must not prefetch Name, ValuePattern value, or TextPattern
text before deciding whether text collection is permitted. Only an explicitly
false password property permits name/value collection. True or unknown emits
neither name nor value and skips that node's descendants. TextPattern/document
scraping is outside v1. Nonpassword ValuePattern values are optional; unsupported
properties are omitted, not invented. A denied property must not silently
become a false password flag.

Fixed limits are 256 nodes, depth 12, names 160 characters, values 256 characters,
and 8,192 collected name/value characters per result. A 1,500 ms traversal
budget is checked between provider calls. Reaching a bound returns the safely
collected prefix with `truncated: true` and the applicable truncation reasons.
Password redaction alone is normal policy, not truncation. Complete results
have false truncation and an empty reasons array. Failure to establish a usable
root returns `accessibility_unavailable`. Partial trees must never be described
as complete representations of the screen.

## Authority and future integration

Python continues to own local arming, selected HWND/PID, controller instance and
generation, fresh observation IDs, cancellation, exact full-image renewal, and
all native input checks. A Rust screenshot or tree alone authorizes no action.
The first adapter exposes read-only methods and cannot be used as an input
backend. Future semantic action resolution requires its own reviewed contract
binding snapshot, Python authority receipt, fresh control identity and bounds;
v1 deliberately exposes no InvokePattern or text-setting method.

A later explicit text-first runner may send the bounded control tree to the
model and use an image or crop when controls are unavailable or insufficient.
Names and values remain untrusted application data. Full-frame authority stays
local. A compact tree is not a generated narration and cannot establish that a
canvas, custom widget, or unlabeled control has been understood.

## Measurement boundaries

The existing deployed Strata image path already caches embeddings using the
SHA-256 of normalized image bytes (`serve/server.py:2932`). A cache hit skips
encoding, while image markers still expand into image prompt tokens
(`serve/server.py:5080`). Image identity participates in conversation checkpoint
validation (`src/core/conversation_state.cpp:92`). These are source findings,
not a new measured speedup from this native core.

The local engine exports actual top-level `timings`: cache/prompt token counts,
prompt milliseconds, predicted token count/milliseconds, and rates
(`serve/server.py:5895`, response forwarding at `6335`). No separate vision
encoding duration was identified. Benchmark unavailable fields stay unavailable;
wall-clock subtraction is not a substitute. The semantic comparison must report
actual usage, prompt timing, request latency, and task accuracy on the same owned
fixture before making a prefill or latency claim.
