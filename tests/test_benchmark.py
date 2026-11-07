"""Offline benchmark guards. No model, GUI, or native input is exercised."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import benchmark_observation as benchmark
from scripts import model_smoke


class ObservationBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        stream = io.BytesIO()
        Image.new("RGB", (4, 3), "#102030").save(stream, format="PNG")
        self.png = stream.getvalue()
        digest = hashlib.sha256(self.png).hexdigest()
        window = {"id": "owned-fixture", "pid": 123, "rect": {"x": 0, "y": 0, "width": 4, "height": 3}}
        observation = {"window": window, "observation_id": "observed", "mime_type": "image/png",
                       "sha256": digest, "image_file": "native.png", "received_at": 1}
        receipt = {"passed": True, "locally_authorized": True, "fixture_pid": 123,
                   "window": window, "observations": [observation]}
        transcript = {"turns": [{"step": 1, "request_started_at": 1, "response_received_at": 2,
            "response": {"content": "Fixture response"}, "model_visible_image": {
                "image_file": "model.png", "sha256": digest, "observation_id": "observed"}}]}
        (self.directory / "model.png").write_bytes(self.png)
        (self.directory / "native.png").write_bytes(self.png)
        (self.directory / "model-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        (self.directory / "model-transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
        self.state = {"image_base64": base64.b64encode(self.png).decode("ascii")}

    def test_default_summary_never_contacts_model_or_readiness(self):
        with patch("sys.argv", ["benchmark", "--baseline", str(self.directory)]), \
                patch.object(benchmark.model_client, "_complete") as complete, \
                patch.object(benchmark, "check_model_ready") as ready, redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.main(), 0)
        complete.assert_not_called()
        ready.assert_not_called()

    def test_input_provenance_rejects_changed_image_and_path_escape(self):
        self.assertEqual(benchmark.load_fixed_observation(self.directory)["image_base64"], self.state["image_base64"])
        (self.directory / "model.png").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            benchmark.load_fixed_observation(self.directory)
        with self.assertRaises(ValueError):
            benchmark._artifact_file(self.directory, "../private.png")

    def test_live_requires_explicit_opt_in(self):
        with patch.object(benchmark.model_client, "_complete") as complete:
            with self.assertRaises(ValueError):
                benchmark.run_read_only_abba(self.state, "http://127.0.0.1:1/v1", "fixture", self.directory)
        complete.assert_not_called()

    def test_live_cli_requires_explicit_model_and_endpoint(self):
        with patch("sys.argv", ["benchmark", "--baseline", str(self.directory), "--run"]), \
                patch.object(benchmark.model_client, "_complete") as complete, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                benchmark.main()
        self.assertEqual(error.exception.code, 2)
        complete.assert_not_called()

    def test_payload_measurement_uses_actual_reasoning_wire_contract(self):
        state = benchmark.load_fixed_observation(self.directory)
        auto = benchmark.prepare_payload(state, "fixture", "baseline", 0, "auto")
        off = benchmark.prepare_payload(state, "fixture", "efficient", 1280, "off")
        auto_body = benchmark.model_client.build_model_request("fixture", auto["messages"], auto["tools"], reasoning="auto")
        off_body = benchmark.model_client.build_model_request("fixture", off["messages"], off["tools"], reasoning="off")
        self.assertNotIn("chat_template_kwargs", auto_body)
        self.assertEqual(off_body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(off["measurement"]["request_bytes"],
                         len(benchmark.model_client.encode_model_request(off_body, performance_profile="efficient")))

    def test_fixture_cli_forwards_explicit_fast_settings_without_running_gui(self):
        arguments = ["model_smoke", "--run", "--endpoint", "http://127.0.0.1:1/v1", "--model", "fixture",
                     "--performance-profile", "efficient", "--max-image-edge", "640", "--reasoning", "off"]
        with patch("sys.argv", arguments), patch.object(model_smoke.sys, "platform", "win32"), \
                patch.object(model_smoke, "run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(model_smoke.main(), 0)
        self.assertEqual(run.call_args.args[-3:], ("efficient", 640, "off"))

    def test_authorized_abba_is_four_serial_responses_without_tool_dispatch(self):
        prepared = {"image": self.png, "measurement": {}, "messages": [], "tools": []}
        with patch.object(benchmark, "check_model_ready", return_value={"loaded": True}) as ready, \
                patch.object(benchmark, "prepare_payload", return_value=prepared), \
                patch.object(benchmark.model_client, "_complete", return_value={"tool_calls": [
                    {"function": {"name": "type_text"}}]}) as complete, \
                patch("opencomputer.mcp.LocalClient.call", side_effect=AssertionError("Tool dispatch is forbidden")):
            result = benchmark.run_read_only_abba(self.state, "http://127.0.0.1:1/v1", "fixture",
                                                 self.directory, authorized=True, efficient_reasoning="off")
        self.assertTrue(result["completed"])
        self.assertFalse(result["tool_dispatch"])
        self.assertEqual(ready.call_count, 4)
        self.assertEqual([call.kwargs["performance_profile"] for call in complete.call_args_list],
                         ["baseline", "efficient", "efficient", "baseline"])
        self.assertEqual([call.kwargs["reasoning"] for call in complete.call_args_list],
                         ["auto", "off", "off", "auto"])

    def test_busy_endpoint_aborts_before_any_model_request(self):
        with patch.object(benchmark, "check_model_ready", side_effect=RuntimeError("Busy")), \
                patch.object(benchmark.model_client, "_complete") as complete:
            result = benchmark.run_read_only_abba(self.state, "http://127.0.0.1:1/v1", "fixture",
                                                 self.directory, authorized=True)
        self.assertFalse(result["completed"])
        self.assertEqual(result["calls"], [])
        complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
