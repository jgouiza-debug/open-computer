"""Measure saved fixture budgets; --run explicitly enables read-only model comparisons.

Default operation never contacts a model or desktop.
Missing server token usage stays unavailable. PNG bytes, JSON bytes, pixels, and
wall-clock timing are distinct measurements, never estimates of vision tokens.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import time
import uuid

from PIL import Image

from opencomputer import model_client
from opencomputer.server import tool_definitions

FIXED_SYSTEM = (
    "Inspect only this saved disposable Windows fixture image. Propose one next "
    "tool call or a concise final answer for the given task. Coordinates refer to "
    "the displayed image. This benchmark records responses and never executes tools."
)
FIXED_TASK = (
    "In the test fixture, type exactly 'Open Computer model test' into the empty "
    "field, then click 'Test click' exactly once. Propose only the next action."
)


def check_model_ready(endpoint: str) -> dict:
    """Reuse the fixture's fail-closed, read-only loaded/idle checks."""
    script = Path(__file__).with_name("model_smoke.py")
    spec = importlib.util.spec_from_file_location("opencomputer_benchmark_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.endpoint_readiness(endpoint)


def _artifact_file(directory: Path, filename: str) -> Path:
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Artifact image must remain within its run directory")
    return path


def _image_metrics(path: Path) -> dict:
    png = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
    return {"width": width, "height": height, "pixels": width * height,
            "png_bytes": len(png), "base64_chars": 4 * ((len(png) + 2) // 3),
            "sha256": hashlib.sha256(png).hexdigest()}


def _timing_summary(values: list[float]) -> dict:
    return {"count": len(values), "total_ms": round(sum(values), 3),
            "median_ms": round(statistics.median(values), 3) if values else None,
            "min_ms": round(min(values), 3) if values else None,
            "max_ms": round(max(values), 3) if values else None}


def summarize_run(directory: Path) -> dict:
    receipt = json.loads((directory / "model-receipt.json").read_text(encoding="utf-8"))
    transcript = json.loads((directory / "model-transcript.json").read_text(encoding="utf-8"))
    calls = []
    for turn in transcript.get("turns", []):
        image_record = turn.get("model_visible_image", {})
        image = _image_metrics(_artifact_file(directory, image_record["image_file"])) if image_record else None
        start, end = turn.get("request_started_at"), turn.get("response_received_at")
        elapsed = (end - start) * 1000 if isinstance(start, (float, int)) and isinstance(end, (float, int)) else None
        calls.append({"step": turn.get("step"), "response_received": "response" in turn,
                      "wrapper_elapsed_ms": round(elapsed, 3) if elapsed is not None else None,
                      "image": image,
                      "saved_hash_matches": image["sha256"] == image_record.get("sha256") if image else None})
    metric_path = directory / "metrics.json"
    metrics = json.loads(metric_path.read_text(encoding="utf-8")) if metric_path.exists() else {}
    events = metrics.get("events", []) if isinstance(metrics, dict) else metrics
    model_events = [event for event in events if event.get("phase") == "model"]
    stages = {}
    for phase in ("observation", "model", "action"):
        values = [float(event["elapsed_ms"]) for event in events
                  if event.get("phase") == phase and isinstance(event.get("elapsed_ms"), (float, int))]
        stages[phase] = _timing_summary(values) if values else None
    reported = [event["server_usage"] for event in model_events
                if event.get("usage_source") == "server" and isinstance(event.get("server_usage"), dict)]
    usage = None
    if reported:
        usage = {"reported_calls": len(reported), "model_metric_events": len(model_events),
                 "totals_for_reported_calls": {key: sum(item[key] for item in reported if key in item)
                     for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens")
                     if any(key in item for item in reported)}}
    request_bytes = [event["request_bytes"] for event in model_events
                     if isinstance(event.get("request_bytes"), int)]
    return {"run": directory.name, "recorded_passed": receipt.get("passed") is True,
            "response_count": sum(call["response_received"] for call in calls),
            "request_attempt_count": len(calls), "calls": calls,
            "wrapper_model_calls": _timing_summary([call["wrapper_elapsed_ms"] for call in calls
                                                    if call["wrapper_elapsed_ms"] is not None]),
            "runner_elapsed_ms": receipt.get("runner_elapsed_ms"),
            "model_request_bytes": {"measured_calls": len(request_bytes), "total": sum(request_bytes)} if request_bytes else None,
            "stage_timings": stages, "server_token_usage": usage,
            "usage_source": "server" if reported else "unavailable",
            "limits": ["Wrapper call timing includes fixture logging overhead.",
                       "Old runs without metrics cannot reconstruct exact request bytes or token usage.",
                       "Image pixels and encoded bytes are not vision-token counts.",
                       "A fixture pass does not establish arbitrary-app reliability."]}


def load_fixed_observation(directory: Path) -> dict:
    """Accept only the first exact image from a recorded successful owned fixture."""
    receipt = json.loads((directory / "model-receipt.json").read_text(encoding="utf-8"))
    transcript = json.loads((directory / "model-transcript.json").read_text(encoding="utf-8"))
    if receipt.get("passed") is not True or receipt.get("locally_authorized") is not True:
        raise ValueError("A locally authorized successful fixture receipt is required")
    record = transcript["turns"][0]["model_visible_image"]
    png = _artifact_file(directory, record["image_file"]).read_bytes()
    digest = hashlib.sha256(png).hexdigest()
    if digest != record.get("sha256"):
        raise ValueError("Saved model-visible image hash does not match its receipt")
    observation = next((item for item in receipt.get("observations", [])
                        if item.get("observation_id") == record.get("observation_id")
                        and item.get("sha256") == digest), None)
    if observation is None or observation.get("window", {}).get("pid") != receipt.get("fixture_pid"):
        raise ValueError("Image lacks matching owned-fixture observation provenance")
    if observation.get("window", {}).get("id") != receipt.get("window", {}).get("id"):
        raise ValueError("Image window identity does not match the fixture receipt")
    original = _artifact_file(directory, observation["image_file"]).read_bytes()
    if original != png:
        raise ValueError("Initial benchmark image must match its native fixture original")
    state = {key: value for key, value in observation.items()
             if key not in {"received_at", "sha256", "image_file", "local_timing"}}
    state["image_base64"] = base64.b64encode(png).decode("ascii")
    return state


def prepare_payload(state: dict, model: str, profile: str, max_image_edge: int,
                    reasoning: str = "auto") -> dict:
    """Use the runner's actual image, schema, message, and serialization helpers."""
    edge = 0 if profile == "baseline" else max_image_edge
    view = model_client.prepare_image_view(state, max_image_edge=edge)
    definitions = [spec for spec in tool_definitions() if spec["name"] in {"click", "type_text", "stop"}]
    tools = model_client.build_model_tools(definitions, performance_profile=profile)
    messages = [{"role": "system", "content": FIXED_SYSTEM},
                {"role": "user", "content": FIXED_TASK},
                model_client._capture_message(state, performance_profile=profile, view=view)]
    body = model_client.build_model_request(model, messages, tools, reasoning=reasoning)
    encoded = model_client.encode_model_request(body, performance_profile=profile)
    image = base64.b64decode(view.image_base64, validate=True)
    measurement = {"profile": profile, "max_image_edge": edge, "requested_reasoning": reasoning,
        "source_width": view.source_width, "source_height": view.source_height,
        "image_width": view.image_width, "image_height": view.image_height,
        "image_pixels": view.image_width * view.image_height,
        "image_png_bytes": len(image), "image_base64_chars": len(view.image_base64),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "request_bytes": len(encoded), "request_sha256": hashlib.sha256(encoded).hexdigest(),
        "message_count": len(messages), "tool_count": len(tools),
        "vision_token_count": None}
    return {"messages": messages, "tools": tools, "image": image, "measurement": measurement}


def offline_payloads(state: dict, model: str, directory: Path,
                     edges: list[int] | None = None, efficient_reasoning: str = "auto") -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    settings = [("baseline", 0)] + [("efficient", edge) for edge in (edges or [1280, 640])]
    for profile, edge in settings:
        prepared = prepare_payload(state, model, profile, edge,
                                   "auto" if profile == "baseline" else efficient_reasoning)
        image_file = f"payload-{profile}-edge{edge}.png"
        (directory / image_file).write_bytes(prepared["image"])
        result.append({**prepared["measurement"], "image_file": image_file})
    return result


def run_read_only_abba(state: dict, endpoint: str, model: str, directory: Path, *,
                       max_image_edge: int = 1280, order: str = "ABBA",
                       authorized: bool = False, efficient_reasoning: str = "auto") -> dict:
    """Explicitly opted-in serial completions. No tool-dispatch client exists here."""
    if not authorized:
        raise ValueError("Live completions require explicit --run authorization")
    if order not in {"AB", "ABBA"}:
        raise ValueError("Order must be AB or ABBA")
    if efficient_reasoning not in {"auto", "off"}:
        raise ValueError("Efficient reasoning must be auto or off")
    directory.mkdir(parents=True, exist_ok=True)
    result = {"order": order, "calls": [], "tool_dispatch": False,
              "fixed_source_sha256": hashlib.sha256(base64.b64decode(state["image_base64"])).hexdigest(),
              "completed": False,
              "limits": ["Small serial sample; cache warm-up and order affect latency.",
                         "Same fixed system/task across profiles; not a reconstruction of historical requests.",
                         "No response is dispatched; this does not establish desktop task quality.",
                         "Tokens are server-reported units; image bytes/pixels are separate."]}

    def save() -> None:
        (directory / "read-only-abba.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    try:
        for index, label in enumerate(order, 1):
            profile = "baseline" if label == "A" else "efficient"
            readiness = check_model_ready(endpoint)
            reasoning = "auto" if label == "A" else efficient_reasoning
            prepared = prepare_payload(state, model, profile, max_image_edge, reasoning)
            image_file = f"request-{index:02d}-{profile}.png"
            (directory / image_file).write_bytes(prepared["image"])
            events: list[dict] = []
            call = {"index": index, **prepared["measurement"], "image_file": image_file,
                    "readiness": readiness, "status": "started"}
            result["calls"].append(call)
            save()
            started = time.perf_counter()
            try:
                response = model_client._complete(endpoint, model, prepared["messages"], prepared["tools"],
                    metrics_callback=events.append, performance_profile=profile, reasoning=reasoning)
                response_file = f"response-{index:02d}-{profile}.json"
                (directory / response_file).write_text(json.dumps(response, indent=2), encoding="utf-8")
                call.update(status="ok", response_file=response_file,
                    proposed_tools=[item.get("function", {}).get("name") for item in response.get("tool_calls", [])])
            except Exception as exc:
                call.update(status="error", error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                call["wrapper_elapsed_ms"] = (time.perf_counter() - started) * 1000
                call["metrics"] = events
                save()
        result["completed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        save()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Saved model-smoke run directory")
    parser.add_argument("--output", type=Path, help="Write measured summary JSON")
    parser.add_argument("--payload", action="store_true", help="Offline request/image budgets using the saved owned fixture")
    parser.add_argument("--run", action="store_true", help="Authorize read-only model completions; never dispatch tools")
    parser.add_argument("--order", choices=("AB", "ABBA"), default="ABBA")
    parser.add_argument("--endpoint", help="Required with --run: explicitly choose the existing loopback service")
    parser.add_argument("--model", help="Required with --run; offline default is benchmark-model")
    parser.add_argument("--max-image-edge", type=int, default=1280, help="Efficient live cap; baseline always native")
    parser.add_argument("--efficient-reasoning", choices=("auto", "off"), default="auto",
                        help="B-only template extension request; A stays auto")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/performance"))
    options = parser.parse_args()
    if options.run and (not options.endpoint or not options.model):
        parser.error("--run requires explicit --endpoint and --model")
    model = options.model or "benchmark-model"
    try:
        if options.max_image_edge != 0 and not 128 <= options.max_image_edge <= 8192:
            raise ValueError("Image edge must be 0 or between 128 and 8192")
        result = summarize_run(options.baseline)
        if options.payload or options.run:
            state = load_fixed_observation(options.baseline)
            artifact_directory = options.artifacts / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
            result["artifact_directory"] = str(artifact_directory)
            result["offline_payloads"] = offline_payloads(state, model, artifact_directory,
                sorted({1280, options.max_image_edge}, reverse=True), options.efficient_reasoning)
            if options.run:
                result["read_only_abba"] = run_read_only_abba(state, options.endpoint, model,
                    artifact_directory, max_image_edge=options.max_image_edge, order=options.order,
                    authorized=True, efficient_reasoning=options.efficient_reasoning)
            (artifact_directory / "benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Cannot summarize fixture artifacts: {exc}\n")
    rendered = json.dumps(result, indent=2)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if options.run and not result.get("read_only_abba", {}).get("completed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
