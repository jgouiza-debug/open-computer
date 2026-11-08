# Performance measurements

These measurements come from one local Windows setup on November 7, 2026.
They separate capture cost, request size, server-reported model tokens, and model
time. [The measured counters are available as JSON](performance-results.json).
They do not predict performance on other models or machines.

## Measured model comparison

The final read-only comparison used the same saved, owned fixture image, task,
system instruction, and allowed actions. Requests ran serially in **ABBA** order:

- **A:** baseline metadata/tools, native 720×440 image, automatic reasoning.
- **B:** compact metadata/tools, 640×391 model view, explicit reasoning off on
  the compatible local template.

No proposed tool call was executed by this benchmark. All four responses proposed
the exact same text-entry arguments.

| Measurement | A, mean of two | B, mean of two |
|---|---:|---:|
| Server prompt tokens | 1,161 | 813 |
| Server completion tokens | 340 | 70 |
| Server total tokens | 1,501 | 883 |
| Model transport time | 6.46 s | 3.74 s |
| Request bytes | 16,687 | 54,056 |

This sample used 29.97% fewer prompt tokens, 41.17% fewer total tokens, and about
42.0% less mean model time. It combined several changes, so it does not isolate
their individual contributions. Its request bytes increased: downsampling this
flat UI introduced antialiased pixels that compressed less effectively as PNG.
Image pixels and encoded bytes are not estimates of vision tokens.

| Order | Mode | Model seconds | Completion tokens | Reported cached prompt tokens |
|---|---|---:|---:|---:|
| A1 | native/auto | 5.343 | 276 | 1,156 |
| B1 | 640/off | 5.015 | 70 | 0 |
| B2 | 640/off | 2.470 | 70 | 806 |
| A2 | native/auto | 7.569 | 404 | 1,156 |

The sample is small, cache states differ, and automatic reasoning produced
different output lengths. An earlier 640px comparison with automatic reasoning
was **slower**: 12.61 s versus 7.60 s despite fewer prompt tokens. A native-image
compaction comparison reduced prompt tokens from 1,161 to 931, with variable
timing. Resizing alone is not a demonstrated general speed improvement.

## Actual native task evidence

The explicit **efficient/640/off** configuration entered the exact test phrase,
clicked the fixture button once, then selected Stop across three real model
responses. The fixture independently verified its text, counter, action evidence,
and overlay flags; control ended stopped and disarmed.

The original harness incorrectly marked that run failed solely because its
terminal result was `Stopped`. The original receipt remains unchanged. A separate
assessment applies the corrected pure verdict, with regressions ensuring early
stops, errors, and local cancellation still fail. No model replay was used to
change the assessment.

Model calls totaled **25.622 s**; runner time was **26.799 s**. Observation stages
settled in 284, 345, and 353 ms after redundant status round trips were removed.
An older three-response run took 31.296 s of model time, but it was not a
controlled comparison and did not retain token-usage counters.

These profiles apply to the **example direct model runner**. External MCP clients
retain the native screenshot and coordinate contract and may perform their own
image processing. Full-resolution native observations remain the authority for
freshness and unchanged-screen checks; model-view coordinates map back to them.

## Where semantic observations save work

Image input is already processed before the language model generates its answer.
For example, [vLLM's multimodal prefix-caching design](https://docs.vllm.ai/en/stable/design/prefix_caching/)
describes image placeholders being replaced by image embeddings during prefill.
Moving an image "into prefill" alone therefore does not remove its vision work.

The optional semantic path instead reads Windows accessibility data locally and
sends a compact text table. For covered controls, this avoids sending an image to
the model at all. The model still prefills the text and generates its tool call.
Canvas content or missing accessibility labels may still require an image.

Prefix caching is a separate inference-engine feature: a repeated prompt prefix
may reuse computation, while changed observation content requires fresh work.
Open Computer does not claim a portable KV-cache import API or force an engine's
cache settings. The benchmark records available server timing/usage counters,
checks answer accuracy, and keeps capture time, payload bytes, tokens and model
latency separate. A Rust implementation alone does not establish a faster model
response; actual semantic-versus-image measurements are required.

## Capture-stage cache

A separate read-only native fixture measured unchanged-frame capture with 20
samples per case. Each call still captures fresh pixels and checks window
identity and geometry. Exact matching pixels can reuse the previous PNG/base64
encoding, within a bounded single-frame cache.

| Native image | Uncached median | Cached median |
|---|---:|---:|
| 720×440 | 7.521 ms | 3.452 ms |
| 1280×800 | 19.696 ms | 9.124 ms |
| 1920×1080 | 36.128 ms | 18.261 ms |

This improves an unchanged-frame capture stage by about 2× in that fixture.
It does not establish a 2× model or complete-task speedup. Changing frames still
need encoding. No Rust performance result is included; inference remains a
separate stage measured in seconds.

## Reproduce without accidental model calls

By default the benchmark reads saved artifacts only:

```powershell
python scripts/benchmark_observation.py --baseline artifacts/model-smoke/YOUR_RUN --payload --max-image-edge 640
```

It saves exact model-view PNGs and request budgets for inspection. Live comparisons
require an explicit `--run`, `--endpoint`, and `--model`. They check loaded/idle
readiness before each request and never dispatch responses as desktop actions.
Use a compatible server before explicitly choosing `--efficient-reasoning off`.

Portable runner defaults remain **1280px/automatic reasoning**. The
`--reasoning off` option requests `chat_template_kwargs.enable_thinking=false`;
it is supported by the tested Strata/template integration and is not a universal
OpenAI-compatible API feature. Metrics record the requested setting rather than
assuming every provider honored it. Use `--metrics-json PATH` on the direct runner
to retain actual model, observation, and action timings and reported usage.
