# Experimental Rust helper validation

On November 7, 2026, the release-built Rust helper was tested against a disposable
Win32 fixture created by the test process. No model requests or input actions were
performed. The fixture closed cleanly after the checks.

| Check | Observed result |
| --- | --- |
| Client size | 720 × 440 pixels |
| Rust versus Python capture | All 10 captures matched decoded RGB pixels exactly |
| Fresh-frame encoding cache | 9 hits after the initial capture |
| Rust capture median, including child IPC and validation | 3.003 ms |
| Existing Python capture median | 3.212 ms |
| Accessibility snapshot | 9 nodes, 113 name/value characters, no truncation |
| Accessibility inspection | 37.041 ms for this one snapshot |
| Password control | Present without name/value; dummy password text absent |
| Incorrect expected process ID | Rejected with `pid_mismatch` |

These are small, sequential measurements on one static window. They establish
native functionality, not a general Rust speed advantage. UI Automation was
slower than capture on this fixture; its potential benefit is reducing model
input. Character counts are not tokenizer counts. No image-versus-semantic model
latency or token improvement has been measured for this helper yet.

The Python controller remains the production input authority. The optional helper
provides observation only; it has no click, typing, or arming operation. The
production model runner still uses images.

For a disposable window with standard Windows controls:

```powershell
python scripts/win32_semantic_fixture.py --run --with-password --timeout 180
```

It prints its PID, HWND, and metadata path. Use those identities with the
[native adapter contract](native-protocol.md). Metadata is discovery information;
the helper must verify live identity and geometry on every observation. Do not
treat a saved snapshot as permission to act.

## Image versus semantic input benchmark

`scripts/benchmark_semantic.py` compares a full image with compact accessibility
text in A/B/B/A order. Both use the same task and reporting tool schema. Returned
tool calls are checked for correct observations and are never executed.

An offline run using a real captured fixture produced an 8,105-byte image request
and a 1,815-byte semantic request, with identical task and tool schema. The nine
controls use 698 characters in a columns-and-rows format that avoids repeating
field names. That is about 78% fewer request bytes on this fixture, **not** a
measured reduction in model tokens or latency. No model request was made in this
offline check.

Start the disposable fixture above, then pass its printed metadata path while it
is still open. With an already-loaded, idle local model:

```powershell
python scripts/benchmark_semantic.py --run --fixture-metadata "PATH/fixture.json" --native-exe "ABSOLUTE/PATH/opencomputer-native.exe" --endpoint "http://127.0.0.1:PORT/v1" --model "MODEL_ID"
```

The command does not start or load a model. The benchmark requires `/health`
loaded-state and `/slots` idle-state checks; servers without those endpoints are
rejected. This restriction applies to the benchmark, not the general
OpenAI-compatible model runner. If the provider supports
`chat_template_kwargs.enable_thinking=false`, add `--reasoning off` to apply it
equally to both representations. Without `--run`, the command opens no window and
makes no model request; `--input-json` validates a saved observation bundle offline.

Results include correctness, request size, wall time, and actual server-reported
usage/timings when available. Missing counters remain unavailable. Changed window
identity, geometry, pixels, or accessibility content aborts the comparison.
Incomplete trees also abort. Cache state and generated output length can affect
the comparison, so a small ABBA run alone cannot establish a general speedup.
