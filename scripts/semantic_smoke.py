"""Opt-in owned-fixture semantic MCP proof with recorded, simulated input only.

No arguments or --help performs no native work. --run reads only a separately
started win32_semantic_fixture window; it never starts a model or injects input.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import threading
from datetime import datetime
import uuid

from opencomputer.backend import BackendCancelled, BackendError, WindowsBackend
from opencomputer.controller import Controller
from opencomputer.mcp import ClientError, LocalClient, content_blocks
from opencomputer.server import start_server
from opencomputer.setup import validate_native_helper
if __package__:
    from scripts.benchmark_semantic import (BenchmarkError, EXPECTED_LABEL, EXPECTED_TITLE,
                                            read_json, safe_error, validate_fixture)
else:
    from benchmark_semantic import (BenchmarkError, EXPECTED_LABEL, EXPECTED_TITLE,
                                    read_json, safe_error, validate_fixture)


class SpyInputBackend:
    """Delegate only owned-fixture inspection/capture; perform never delegates."""

    real = False

    def __init__(self, metadata: dict, reader) -> None:
        self.target = validate_fixture(metadata, live=True)
        self.reader = reader
        self.cancel_event = threading.Event()
        self.actions: list[dict] = []
        self.pinned = None

    def set_cancel_event(self, event: threading.Event) -> None:
        self.cancel_event = event

    def get_window(self, window_id) -> dict:
        if str(window_id) != self.target["window_id"]:
            raise BackendError("Only the owned fixture may be inspected")
        window = self.reader.get_window(window_id)
        rect = window["rect"]
        identity = {"id": str(window["id"]), "pid": window.get("pid"), "title": window.get("title"),
                    "rect": {key: rect[key] for key in ("x", "y", "width", "height")}}
        if (identity["id"] != self.target["window_id"] or identity["pid"] != self.target["expected_pid"] or
                identity["title"] != EXPECTED_TITLE or
                (rect["width"], rect["height"]) != (720, 440)):
            raise BackendError("Owned fixture process/window identity is invalid")
        if self.pinned is not None and identity != self.pinned:
            raise BackendError("Owned fixture identity or geometry changed")
        self.pinned = identity
        return window

    def list_windows(self) -> list[dict]:
        return [self.get_window(self.target["window_id"])]

    def capture(self, window_id) -> dict:
        self.get_window(window_id)
        result = self.reader.capture(window_id)
        self.get_window(window_id)
        return result

    def perform(self, action, window_id, **params) -> None:
        # This is the only action sink. Do not add a reader.perform call here.
        if self.cancel_event.is_set():
            raise BackendCancelled("Simulated fixture action was cancelled")
        self.get_window(window_id)
        if action != "click":
            raise BackendError("The semantic smoke records clicks only")
        self.actions.append({"action": action, "window_id": str(window_id), "params": params})


def make_provider(helper: Path):
    from opencomputer.semantic import NativeSemanticProvider
    return NativeSemanticProvider(helper)


def inspect_projection(state: dict) -> tuple[dict, tuple[int, int]]:
    """Validate the text-only MCP projection and locate the fixed safe target."""
    try:
        blocks = content_blocks(state)
    except ClientError:
        raise BenchmarkError("Semantic MCP response failed validation") from None
    if len(blocks) != 1 or blocks[0].get("type") != "text":
        raise BenchmarkError("Semantic MCP observations must contain exactly one text block")
    projection = json.loads(blocks[0]["text"])
    if set(projection) != {"observation_id", "width", "height", "semantic"}:
        raise BenchmarkError("Semantic MCP projection contains unexpected fields")
    if (projection["width"], projection["height"]) != (720, 440):
        raise BenchmarkError("Semantic MCP projection has invalid fixture dimensions")
    tree = projection["semantic"]
    columns, rows = tree["columns"], tree["rows"]
    if tree["format"] != "controls-v1" or not isinstance(columns, list) or len(set(columns)) != len(columns):
        raise BenchmarkError("Semantic table format is invalid")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise BenchmarkError("Semantic table row count is invalid")
    nodes = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise BenchmarkError("Semantic table row is invalid")
        node = dict(zip(columns, row, strict=True))
        if node.get("password") is not False and (node.get("name") is not None or node.get("value") is not None):
            raise BenchmarkError("Semantic password text was not redacted")
        nodes.append(node)
    matching = [node for node in nodes if node.get("name") == EXPECTED_LABEL and
                node.get("role") == "button" and node.get("password") is False and node.get("enabled") is True]
    if len(matching) != 1:
        raise BenchmarkError("Semantic fixture target is missing or ambiguous")
    target = matching[0]
    # The live controller owns and revalidates action coordinates, never UIA IDs.
    bounds = {key: target[key] for key in ("x", "y", "width", "height")}
    if bounds != {"x": 32, "y": 126, "width": 150, "height": 40}:
        raise BenchmarkError("Semantic target bounds do not match the owned fixture")
    if not any(node.get("name") == "Clicks: 0" for node in nodes):
        raise BenchmarkError("The fixture is not in its pristine counter state")
    return projection, (107, 146)


def run_smoke(metadata: dict, helper: Path, directory: Path, *, authorized=False) -> dict:
    if not authorized:
        raise BenchmarkError("Native semantic smoke requires explicit --run")
    target = validate_fixture(metadata, live=True)
    helper = validate_native_helper(helper)
    result = {"passed": False, "real_native_observation": False,
              "input_backend": "recording-spy", "native_input_executed": False,
              "model_requests": 0, "fixture_pid": target["expected_pid"],
              "fixture_hwnd": target["window_id"], "simulated_actions": 0}
    controller = provider = server = None
    directory.mkdir(parents=True, exist_ok=True)
    try:
        backend = SpyInputBackend(metadata, WindowsBackend())
        backend.get_window(target["window_id"])
        provider = make_provider(helper)  # Lazy: no helper process until semantic request.
        controller = Controller(backend=backend, semantic_provider=provider)
        controller.select_window(target["window_id"])
        server = start_server(controller, port=0, connection_path=None)
        client = LocalClient(server.url, server.token)
        initial = client.call("status")
        if initial["armed"] is not False:
            raise BenchmarkError("Smoke controller must start disarmed")
        state = client.call("get_semantic_state")
        projection, (x, y) = inspect_projection(state)
        png = base64.b64decode(state["image_base64"], validate=True)
        (directory / "fixture-native.png").write_bytes(png)
        (directory / "semantic-projection.json").write_text(json.dumps(projection, indent=2), encoding="utf-8")
        result.update(real_native_observation=True, image_sha256=hashlib.sha256(png).hexdigest(),
                      semantic_rows=len(projection["semantic"]["rows"]))
        if backend.actions:
            raise BenchmarkError("Observation unexpectedly invoked a simulated action")
        # Only this real=False backend is locally armed; its action sink records.
        controller.local_arm()
        armed_state = client.call("get_semantic_state")
        inspect_projection(armed_state)
        client.call("click", {"observation_id": armed_state["observation_id"], "x": x, "y": y})
        if len(backend.actions) != 1:
            raise BenchmarkError("Expected exactly one recorded click")
        result["simulated_actions"] = 1
        replay_rejected = False
        try:
            client.call("click", {"observation_id": armed_state["observation_id"], "x": x, "y": y})
        except Exception as exc:
            replay_rejected = getattr(exc, "code", None) in {"stale_observation", "observation_used", "invalid_observation"}
        if not replay_rejected or len(backend.actions) != 1:
            raise BenchmarkError("Semantic observation replay was not rejected")
        final_projection, _ = inspect_projection(client.call("get_semantic_state"))
        if final_projection["semantic"] != projection["semantic"]:
            raise BenchmarkError("Owned fixture controls changed during simulated input")
        result.update(replay_rejected=True, fixture_controls_unchanged=True, passed=True)
    except Exception as exc:
        result["error"] = safe_error(exc)
    finally:
        try:
            if controller is not None:
                controller.stop()
        finally:
            try:
                if server is not None:
                    server.close()
            finally:
                if controller is not None:
                    controller.close()
                elif provider is not None:
                    provider.close()
        result["closed"] = True
        (directory / "semantic-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize owned-fixture reads and simulated input routing")
    parser.add_argument("--fixture-metadata", type=Path)
    parser.add_argument("--native-exe", type=Path)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/semantic-smoke"))
    options = parser.parse_args(argv)
    if not options.run:
        parser.print_help()
        return 0
    if options.fixture_metadata is None or options.native_exe is None:
        parser.error("--run requires explicit --fixture-metadata and --native-exe")
    if options.fixture_metadata.name != "fixture.json":
        parser.error("Use the owned Win32 fixture's fixture.json metadata")
    try:
        directory = options.artifacts / (datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        result = run_smoke(read_json(options.fixture_metadata), options.native_exe, directory, authorized=True)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": safe_error(exc)}))
        return 1
    print(json.dumps({**result, "artifact_directory": str(directory)}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
