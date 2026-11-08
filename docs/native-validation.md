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
