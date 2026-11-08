"""Fake-only semantic benchmark tests: no desktop, observer or model is used."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import benchmark_semantic as benchmark
from scripts import win32_semantic_fixture as fixture


def bundle(with_password=True):
    controls = [{**item, "hwnd": 1000 + index} for index, item in
                enumerate(fixture.control_specs(with_password))]
    metadata = fixture.fixture_metadata(123, 456, controls)
    metadata["timeout_seconds"] = 180
    window = {"id": "456", "pid": 123, "title": benchmark.EXPECTED_TITLE,
              "rect": {"x": 80, "y": 100, "width": 720, "height": 440}}
    stream = io.BytesIO()
    Image.new("RGB", (720, 440), "white").save(stream, format="PNG")
    capture = {"window": window, "mime_type": "image/png", "image_width": 720,
               "image_height": 440, "image_base64": base64.b64encode(stream.getvalue()).decode(),
               "metrics": {"capture_ms": 3, "encode_ms": 1, "cache_hit": False}}
    nodes = [{"id": "e0", "parent": None, "role": "window", "name": window["title"],
              "bounds": {"x": 0, "y": 0, "width": 720, "height": 440},
              "enabled": True, "focusable": False, "password": False}]
    for index, item in enumerate(metadata["controls"], 1):
        node = {"id": f"e{index}", "parent": "e0",
                "role": {"STATIC": "text", "EDIT": "edit", "BUTTON": "button"}[item["class_name"]],
                "bounds": item["bounds"], "enabled": True,
                "focusable": item["class_name"] != "STATIC", "password": item["password"]}
        if not item["password"]:
            node["name"] = item["label"]
        nodes.append(node)
    accessibility = {"snapshot_id": "fake-snapshot", "window": window, "nodes": nodes,
                     "truncated": False, "truncation_reasons": [], "metrics": {"inspect_ms": 4}}
    return {"fixture_metadata": metadata, "capture": capture, "accessibility": accessibility}


class FakeObserver:
    def __init__(self, data):
        self.data, self.calls, self.closed = data, [], False

    def call(self, method, **target):
        self.calls.append((method, target))
        return deepcopy({"inspect_window": self.data["capture"]["window"],
                         "capture_window": self.data["capture"],
                         "inspect_accessibility": self.data["accessibility"]}[method])

    def close(self):
        self.closed = True


def response(label="Test click", counter=0):
    return {"tool_calls": [{"type": "function", "function": {"name": "report_target",
            "arguments": json.dumps({"primary_label": label, "counter": counter})}}]}


class SemanticBenchmarkTests(unittest.TestCase):
    def observation(self, data=None):
        data = data or bundle()
        return benchmark.validate_bundle(data["fixture_metadata"], data["capture"], data["accessibility"])

    def test_no_arguments_and_help_cannot_start_native_or_model(self):
        with patch.object(benchmark.native_client, "NativeClient") as native, \
                patch.object(benchmark.model_client, "_complete") as model, \
                patch.object(benchmark, "check_model_ready") as ready, redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.main([]), 0)
            with self.assertRaises(SystemExit):
                benchmark.main(["--help"])
        native.assert_not_called()
        model.assert_not_called()
        ready.assert_not_called()

    def test_offline_never_uses_network_or_observer_and_saves_redacted_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "input.json").write_text(json.dumps(bundle()), encoding="utf-8")
            with patch.object(benchmark.native_client, "NativeClient") as native, \
                    patch.object(benchmark.model_client, "_complete") as model, \
                    patch.object(benchmark, "check_model_ready") as ready, redirect_stdout(io.StringIO()):
                self.assertEqual(benchmark.main(["--input-json", str(path / "input.json"),
                                                "--artifacts", str(path / "output")]), 0)
            native.assert_not_called()
            model.assert_not_called()
            ready.assert_not_called()
            saved = next((path / "output").glob("*/semantic-benchmark.json"))
            report = json.loads(saved.read_text())
            self.assertEqual(report["model_requests"], 0)
            self.assertFalse(report["tool_dispatch"])
            self.assertEqual(report["payloads"][0]["image_pixels"], 720 * 440)
            tree = (saved.parent / "accessibility.json").read_text()
            self.assertNotIn(fixture.PASSWORD_SENTINEL, tree)
            self.assertNotIn('"value"', tree)

    def test_conditions_have_identical_task_schema_and_no_answer_leak(self):
        observation = self.observation()
        image, semantic = [benchmark.prepare_request(observation, "test-model", mode, "off")
                           for mode in ("image", "semantic")]
        self.assertEqual(image["messages"][:2], semantic["messages"][:2])
        self.assertEqual(image["tools"], semantic["tools"])
        prompt = json.dumps([image["messages"][:2], image["tools"]])
        self.assertNotIn("Test click", prompt)
        self.assertNotIn("Clicks: 0", prompt)
        self.assertEqual(semantic["image_pixels"], 0)
        self.assertGreater(semantic["tree_characters"], 0)

    def test_compact_columns_preserve_every_safe_field_with_fewer_bytes(self):
        observation = self.observation()
        compact = json.loads(observation["compact_tree"])
        original = observation["safe_accessibility"]["nodes"]
        reconstructed = []
        for row in compact["rows"]:
            item = dict(zip(compact["columns"], row, strict=True))
            item["bounds"] = {key: item.pop(key) for key in ("x", "y", "width", "height")}
            if item["name"] is None:
                del item["name"]
            reconstructed.append(item)
        self.assertEqual(reconstructed, original)
        verbose = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLess(len(observation["compact_tree"].encode()), len(verbose))

    def test_readiness_cancellation_interrupts_blocked_read_without_network(self):
        cancellation, interrupted = threading.Event(), threading.Event()

        class FakeSocket:
            def shutdown(self, mode):
                interrupted.set()

            def detach(self):
                return 999

        class BlockedConnection:
            def __init__(self, *args):
                self.active_socket = FakeSocket()

            def request(self, *args):
                pass

            def getresponse(self):
                return self

            def read(self, limit):
                interrupted.wait(1)
                raise OSError("cancelled fake read")

            def close(self):
                pass

        timer = threading.Timer(0.02, cancellation.set)
        started = time.monotonic()
        with patch.object(benchmark.model_client, "_ModelConnection", BlockedConnection), \
                patch.object(benchmark.model_client.socket, "close"):
            timer.start()
            try:
                with self.assertRaises(OSError):
                    benchmark.check_model_ready("http://127.0.0.1:1/v1",
                        cancel_event=cancellation, deadline=time.monotonic() + 0.02)
            finally:
                timer.cancel()
        self.assertTrue(interrupted.is_set())
        self.assertLess(time.monotonic() - started, 0.5)

    def test_expired_readiness_budget_never_constructs_connection(self):
        with patch.object(benchmark.model_client, "_ModelConnection") as transport:
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.check_model_ready("http://127.0.0.1:1/v1",
                    cancel_event=threading.Event(), deadline=time.monotonic() - 1)
        transport.assert_not_called()

    def test_input_is_bounded_on_actual_read_before_json_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_bytes(b" " * 11)
            with patch.object(benchmark.native_client, "MAX_RESPONSE_BYTES", 10), \
                    patch.object(benchmark.model_client, "_strict_json") as parse:
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.read_json(path)
            parse.assert_not_called()

    def test_wrong_identity_title_and_geometry_reject(self):
        for key, value in (("pid", 999), ("title", "Some other app"),
                           ("rect", {"x": 80, "y": 100, "width": 721, "height": 440})):
            with self.subTest(key=key):
                data = bundle()
                data["capture"]["window"][key] = value
                with self.assertRaises(RuntimeError):
                    self.observation(data)

    def test_partial_and_password_leaks_reject_without_persisting(self):
        mutations = [lambda tree: tree.update(truncated=True, truncation_reasons=["node_limit"]),
                     lambda tree: tree["nodes"][-1].update(name=fixture.PASSWORD_SENTINEL),
                     lambda tree: tree["nodes"][-1].update(value=fixture.PASSWORD_SENTINEL),
                     lambda tree: tree["nodes"][-1].update(password=False)]
        for mutate in mutations:
            data = bundle()
            mutate(data["accessibility"])
            with self.assertRaises(RuntimeError):
                self.observation(data)

    def test_explicit_run_and_all_connection_arguments_required(self):
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.compare_live({}, Path("not-started.exe"), "http://127.0.0.1:1/v1", "model", Path("unused"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            benchmark.main(["--run"])

    def test_live_fake_abba_retains_actual_counters_and_never_dispatches(self):
        data = bundle()
        observer = FakeObserver(data)
        calls = []

        def complete(endpoint, model, messages, tools, **options):
            calls.append((messages, tools, options))
            options["metrics_callback"]({"phase": "model", "usage_source": "server",
                "server_usage": {"prompt_tokens": 123, "completion_tokens": 12, "total_tokens": 135},
                "timings_source": "server", "server_timings": {"prompt_ms": 23, "predicted_ms": 45}})
            return response()

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "check_model_ready", return_value={"loaded": True, "idle": True}) as ready, \
                patch.object(benchmark.model_client, "_complete", side_effect=complete):
            result = benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                reasoning="off", client_factory=lambda *args, **kwargs: observer)
        self.assertTrue(result["completed"])
        self.assertTrue(result["all_answers_correct"])
        self.assertEqual([call["condition"] for call in result["calls"]], list("ABBA"))
        self.assertEqual(ready.call_count, 4)
        self.assertTrue(observer.closed)
        self.assertEqual({method for method, _ in observer.calls},
                         {"inspect_window", "capture_window", "inspect_accessibility"})
        self.assertTrue(all(target == {"window_id": "456", "expected_pid": 123} for _, target in observer.calls))
        self.assertTrue(all(call[2]["reasoning"] == "off" for call in calls))
        self.assertEqual(result["calls"][0]["model_metrics"][0]["server_timings"]["prompt_ms"], 23)
        self.assertIsNone(result["vision_encode_ms"])

    def test_max_tokens_matches_prepared_hash_actual_wire_and_receipts(self):
        data, wire = bundle(), []
        observer = FakeObserver(data)
        observation = self.observation(data)

        class FakeConnection:
            active_socket = None

            def __init__(self, *args):
                pass

            def request(self, method, path, body, headers):
                wire.append(body)

            def getresponse(self):
                return self

            status = 200

            def read(self, limit):
                return json.dumps({"choices": [{"message": response()}]}).encode()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "check_model_ready", return_value={"loaded": True}), \
                patch.object(benchmark.model_client, "_ModelConnection", FakeConnection):
            result = benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                reasoning="off", max_tokens=128, client_factory=lambda *args, **kwargs: observer)
        self.assertTrue(result["completed"])
        self.assertEqual(result["requested_max_tokens"], 128)
        self.assertEqual(len(wire), 4)
        for raw, call in zip(wire, result["calls"], strict=True):
            prepared = benchmark.prepare_request(observation, "test-model", call["representation"],
                                                 "off", max_tokens=128)
            self.assertEqual(json.loads(raw)["max_tokens"], 128)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), prepared["request_sha256"])
            self.assertEqual(call["request_sha256"], prepared["request_sha256"])
            self.assertEqual(call["requested_max_tokens"], 128)
        default = benchmark.prepare_request(observation, "test-model", "semantic")
        self.assertEqual(default["requested_max_tokens"], 1024)
        for cap in (1, 128, 256, 512, 1024):
            prepared = benchmark.prepare_request(observation, "test-model", "semantic", max_tokens=cap)
            self.assertEqual(prepared["requested_max_tokens"], cap)
            self.assertEqual(benchmark.model_client.build_model_request("model", [], [], max_output_tokens=cap)["max_tokens"], cap)

    def test_invalid_max_tokens_reject_before_observer_readiness_or_model_io(self):
        for value in (0, -1, 1025, True, 1.5, "128", None):
            with patch.object(benchmark.native_client, "NativeClient") as native, \
                    patch.object(benchmark, "check_model_ready") as readiness, \
                    patch.object(benchmark.model_client, "_ModelConnection") as transport:
                with self.assertRaises((ValueError, benchmark.BenchmarkError)):
                    benchmark.compare_live({}, Path("unused.exe"), "http://127.0.0.1:1/v1", "model",
                                           Path("unused"), authorized=True, max_tokens=value)
                with self.assertRaises(ValueError):
                    benchmark.model_client._complete("http://127.0.0.1:1/v1", "model", [], [],
                                                     max_output_tokens=value)
            native.assert_not_called()
            readiness.assert_not_called()
            transport.assert_not_called()

    def test_cli_max_tokens_forwards_and_rejects_invalid_without_io(self):
        data = bundle()
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "fixture.json"
            metadata.write_text(json.dumps(data["fixture_metadata"]), encoding="utf-8")
            with patch.object(benchmark, "compare_live", return_value={"completed": True}) as compare, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(benchmark.main(["--run", "--fixture-metadata", str(metadata),
                    "--native-exe", "unused.exe", "--endpoint", "http://127.0.0.1:1/v1", "--model", "fake",
                    "--max-tokens", "256", "--artifacts", temporary]), 0)
            self.assertEqual(compare.call_args.kwargs["max_tokens"], 256)
        for value in ("0", "1025", "1.5"):
            with patch.object(benchmark, "read_json") as read, \
                    patch.object(benchmark, "compare_live") as compare, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    benchmark.main(["--max-tokens", value])
            self.assertEqual(error.exception.code, 2)
            read.assert_not_called()
            compare.assert_not_called()

    def test_busy_endpoint_aborts_before_any_model_request(self):
        data = bundle()
        observer = FakeObserver(data)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "check_model_ready", side_effect=RuntimeError("busy")), \
                patch.object(benchmark.model_client, "_complete") as model:
            result = benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                client_factory=lambda *args, **kwargs: observer)
        self.assertFalse(result["completed"])
        model.assert_not_called()
        self.assertTrue(observer.closed)

    def test_observer_startup_uses_remaining_overall_budget(self):
        data = bundle()
        observer = FakeObserver(data)
        factory = unittest.mock.Mock(return_value=observer)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "check_model_ready", side_effect=RuntimeError("busy")):
            benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                timeout=1, client_factory=factory)
        self.assertGreater(factory.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(factory.call_args.kwargs["timeout"], 1)

    def test_observation_change_aborts_after_first_response_without_replay(self):
        data = bundle()
        observer = FakeObserver(data)

        def changed(*args, **kwargs):
            data["accessibility"]["nodes"][0]["name"] = "Changed fixture state"
            return response()

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "check_model_ready", return_value={"loaded": True}), \
                patch.object(benchmark.model_client, "_complete", side_effect=changed) as model:
            result = benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                client_factory=lambda *args, **kwargs: observer)
        self.assertFalse(result["completed"])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["calls"][0]["status"], "aborted")

    def test_deadline_during_final_guard_cannot_promote_completed(self):
        data = bundle()
        observer = FakeObserver(data)
        observation = self.observation(data)
        shared_event = []
        observations = []

        def capture(*args):
            observations.append(True)
            # Initial plus two guarded observations per model request.
            if len(observations) == 9:
                shared_event[0].set()
            return observation

        def complete(*args, **kwargs):
            shared_event[:] = [kwargs["cancel_event"]]
            return response()

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "observe_owned", side_effect=capture), \
                patch.object(benchmark, "check_model_ready", return_value={"loaded": True}), \
                patch.object(benchmark.model_client, "_complete", side_effect=complete):
            result = benchmark.compare_live(data["fixture_metadata"], Path("fake.exe"),
                "http://127.0.0.1:1/v1", "test-model", Path(temporary), authorized=True,
                client_factory=lambda *args, **kwargs: observer)
        self.assertFalse(result["completed"])
        self.assertEqual(result["calls"][-1]["status"], "aborted")

    def test_response_assessment_requires_one_exact_report_and_checks_both_answers(self):
        self.assertTrue(benchmark.assess_response(response())["correct"])
        self.assertFalse(benchmark.assess_response(response("Decoy"))["correct"])
        self.assertFalse(benchmark.assess_response(response(counter=1))["correct"])
        malformed = response()
        malformed["tool_calls"].append(malformed["tool_calls"][0])
        self.assertFalse(benchmark.assess_response(malformed)["format_valid"])


if __name__ == "__main__":
    unittest.main()
