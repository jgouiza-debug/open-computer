"""MCP protocol and local model boundary tests with deterministic fixtures."""

import io
import base64
import contextlib
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from opencomputer.mcp import ClientError, LocalClient, content_blocks, handle_message, serve_stdio
from opencomputer.model_client import (
    _append_observation, _capture_message, _complete, _model_message, _check_authority, _read_observation,
    _settled_observation, build_model_tools, build_model_request, encode_model_request,
    prepare_image_view, ImageView, run_task, RunCancelled,
    main as model_main,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.observations = 0
        self.armed = True
        self.generation = 0
        self.image = "cG5n"
        self.rect = {"width": 10, "height": 10}
        self.instance_id = "fixture-controller"

    def tools(self) -> list:
        return [{"name": "click", "description": "Click", "inputSchema": {"type": "object"}}]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        if name == "status":
            return {"armed": self.armed, "control_generation": self.generation,
                    "controller_instance_id": self.instance_id,
                    "selected_window": {"id": "fixture", "pid": 7}}
        if name == "get_window_state":
            self.observations += 1
            return {"observation_id": f"obs-{self.observations}", "armed": self.armed, "control_generation": self.generation, "controller_instance_id": self.instance_id, "window": {"id": "fixture", "pid": 7, "rect": dict(self.rect)}, "image_base64": self.image, "mime_type": "image/png"}
        if name == "bad":
            raise ClientError("stale_observation", "Observe again")
        return {"ok": True}


class SemanticClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.semantic_error = None
        self.changed_controls = False

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name == "get_semantic_state":
            self.calls.append((name, arguments))
            if self.semantic_error:
                raise ClientError(self.semantic_error, "Semantic observation rejected")
            self.observations += 1
            state = {"observation_id": f"obs-{self.observations}", "armed": self.armed,
                     "control_generation": self.generation, "controller_instance_id": self.instance_id,
                     "window": self.window(), "image_base64": self.image, "mime_type": "image/png",
                     "observation_kind": "semantic", "semantic_fingerprint": "not-trusted-without-content"}
            from opencomputer.semantic import SEMANTIC_COLUMNS
            state["semantic"] = {"format": "controls-v1", "columns": list(SEMANTIC_COLUMNS),
                "rows": [["e0", None, "window", "Fixture", None, 0, 0, 10, 10, True, False, False],
                         ["e1", "e0", "button", "Changed" if self.changed_controls else "Test click", None,
                          1, 2, 2, 2, True, True, False]]}
            return state
        result = super().call(name, arguments)
        if name == "status":
            result["selected_window"] = self.window()
        elif name == "get_window_state":
            result["window"] = self.window()
        return result

    def window(self) -> dict:
        return {"id": "123", "pid": 7, "title": "Fixture",
                "rect": {"x": 0, "y": 0, **self.rect}}


class McpTests(unittest.TestCase):
    @staticmethod
    def wait_http_ready(port: int) -> None:
        # A completed harmless request proves the listening thread is servicing
        # connections before a cancellation timing assertion starts. Windows can
        # transiently delay the first loopback connection; only this read-only
        # readiness GET may retry, never a model or desktop input request.
        deadline = time.monotonic() + 8.0
        last_cause = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=min(1.0, remaining))
            try:
                connection.request("GET", "/fixture-ready")
                response = connection.getresponse()
                response.read()
                return
            except (OSError, http.client.HTTPException) as exc:
                last_cause = exc
            finally:
                connection.close()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise AssertionError(f"HTTP fixture readiness deadline expired; last cause: {type(last_cause).__name__}: {last_cause}") from last_cause

    def test_readiness_retries_only_get_after_transient_connect_failure(self) -> None:
        failed, ready = Mock(), Mock()
        failed.request.side_effect = TimeoutError("transient startup")
        with patch("http.client.HTTPConnection", side_effect=[failed, ready]) as connections, \
                patch.object(time, "sleep"):
            self.wait_http_ready(1)
        self.assertEqual(connections.call_count, 2)
        failed.request.assert_called_once_with("GET", "/fixture-ready")
        ready.request.assert_called_once_with("GET", "/fixture-ready")
        failed.close.assert_called_once()
        ready.close.assert_called_once()

    def test_readiness_deadline_reports_last_connection_cause(self) -> None:
        connection = Mock()
        connection.request.side_effect = TimeoutError("bounded fixture startup")
        with patch("http.client.HTTPConnection", return_value=connection), \
                patch.object(time, "monotonic", side_effect=[0, 0, 9, 9]), \
                patch.object(time, "sleep"):
            with self.assertRaisesRegex(AssertionError, "TimeoutError: bounded fixture startup"):
                self.wait_http_ready(1)
        connection.request.assert_called_once_with("GET", "/fixture-ready")

    def test_initialize_tools_and_image_blocks(self) -> None:
        client = FakeClient()
        result = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, client)
        self.assertEqual(result["result"]["protocolVersion"], "2025-03-26")
        result = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_window_state"}}, client)
        blocks = result["result"]["content"]
        self.assertEqual(blocks[1], {"type": "image", "data": "cG5n", "mimeType": "image/png"})
        self.assertNotIn("image_base64", blocks[0]["text"])

    def test_semantic_mcp_is_one_text_block_without_hidden_image_or_authority(self) -> None:
        client = SemanticClient()
        result = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "get_semantic_state"}}, client)
        blocks = result["result"]["content"]
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        projection = json.loads(blocks[0]["text"])
        self.assertEqual(set(projection), {"observation_id", "width", "height", "semantic"})
        for private in ("image_base64", "control_generation", "controller_instance_id", "pid", "semantic_fingerprint"):
            self.assertNotIn(private, blocks[0]["text"])

    def test_semantic_tool_is_zero_argument_and_image_request_is_local_only(self) -> None:
        from opencomputer.server import dispatch, tool_definitions, ToolError
        controller = Mock()
        controller.get_semantic_state.return_value = {"ok": True}
        self.assertEqual(dispatch(controller, "get_semantic_state", {}), {"ok": True})
        with self.assertRaises(ToolError):
            dispatch(controller, "get_semantic_state", {"window_id": "123"})
        self.assertNotIn("request_image", {item["name"] for item in tool_definitions()})
        tools = build_model_tools(tool_definitions(), observation_mode="auto")
        self.assertIn("request_image", {item["function"]["name"] for item in tools})

    def test_semantic_mcp_rejects_invalid_or_image_only_service_result(self) -> None:
        client = SemanticClient()
        invalid = client.call("get_semantic_state")
        invalid["semantic"]["rows"][1][-1] = None
        for state in (invalid, {"image_base64": "private-image", "mime_type": "image/png"}):
            client = Mock()
            client.call.return_value = state
            result = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "get_semantic_state"}}, client)
            self.assertTrue(result["result"]["isError"])
            self.assertEqual([item["type"] for item in result["result"]["content"]], ["text"])
            self.assertNotIn("private-image", json.dumps(result))

    def test_semantic_runner_sends_text_only_and_keeps_native_coordinates(self) -> None:
        client = SemanticClient()
        seen = []

        def complete(_endpoint, _model, messages, _tools, **_options):
            content = messages[-1]["content"]
            self.assertIsInstance(content, str)
            self.assertNotIn("image_base64", content)
            self.assertNotIn(client.instance_id, content)
            seen.append(content)
            if len(seen) == 2:
                return {"role": "assistant", "content": "done"}
            return {"tool_calls": [{"id": "click-1", "type": "function", "function": {"name": "click",
                    "arguments": json.dumps({"observation_id": f"obs-{client.observations}", "x": 9, "y": 9})}}]}

        with patch("opencomputer.model_client._complete", side_effect=complete):
            self.assertEqual(run_task(client, "http://127.0.0.1:9/v1", "fake", "Click", max_steps=2,
                                      observation_mode="semantic", max_image_edge=1), "Model response: done")
        clicks = [args for name, args in client.calls if name == "click"]
        self.assertEqual((clicks[0]["x"], clicks[0]["y"]), (9, 9))
        self.assertNotIn("get_window_state", [name for name, _ in client.calls])
        self.assertEqual(client.calls[-1][0], "stop")

    def test_same_pixels_but_changed_semantics_rejects_input(self) -> None:
        client = SemanticClient()

        def complete(*_args, **_kwargs):
            client.changed_controls = True
            return {"tool_calls": [{"id": "click-1", "type": "function", "function": {"name": "click",
                    "arguments": json.dumps({"observation_id": f"obs-{client.observations}", "x": 1, "y": 2})}}]}

        with patch("opencomputer.model_client._complete", side_effect=complete):
            with self.assertRaisesRegex(RuntimeError, "controls changed"):
                run_task(client, "http://127.0.0.1:9/v1", "fake", "Click", observation_mode="semantic")
        self.assertNotIn("click", [name for name, _ in client.calls])

    def test_auto_never_falls_back_or_uses_new_authority_after_model_planning(self) -> None:
        for change in ("unavailable", "epoch"):
            client = SemanticClient()

            def complete(*_args, **_kwargs):
                if change == "unavailable":
                    client.semantic_error = "semantic_unavailable"
                else:
                    client.generation += 1
                return {"tool_calls": [{"id": "click-1", "type": "function", "function": {"name": "click",
                        "arguments": json.dumps({"observation_id": f"obs-{client.observations}", "x": 1, "y": 2})}}]}

            with patch("opencomputer.model_client._complete", side_effect=complete):
                with self.assertRaises((ClientError, RunCancelled)):
                    run_task(client, "http://127.0.0.1:9/v1", "fake", "Click", observation_mode="auto")
            self.assertNotIn("click", [name for name, _ in client.calls])
            self.assertNotIn("get_window_state", [name for name, _ in client.calls])

    def test_auto_fallback_is_limited_to_expected_inadequacy(self) -> None:
        for code in ("semantic_unavailable", "semantic_incomplete", "semantic_empty"):
            client, events = SemanticClient(), []
            client.semantic_error = code
            with patch("opencomputer.model_client._complete", return_value={"content": "done"}):
                run_task(client, "http://127.0.0.1:9/v1", "fake", "Read", observation_mode="auto", metrics_callback=events.append)
            self.assertEqual(sum(name == "get_semantic_state" for name, _ in client.calls), 1)
            self.assertEqual(sum(name == "get_window_state" for name, _ in client.calls), 3)
            self.assertIn({"phase": "observation_fallback", "reason": code, "observation_mode": "image"}, events)
        for code in ("cancelled", "target_changed", "semantic_provider_failed", "capture_failed"):
            client = SemanticClient()
            client.semantic_error = code
            with patch("opencomputer.model_client._complete") as complete:
                with self.assertRaises(ClientError):
                    run_task(client, "http://127.0.0.1:9/v1", "fake", "Read", observation_mode="auto")
            complete.assert_not_called()
            self.assertNotIn("get_window_state", [name for name, _ in client.calls])

    def test_auto_image_request_is_intercepted_and_preserves_history_pair(self) -> None:
        client, seen = SemanticClient(), []

        def complete(_endpoint, _model, messages, _tools, **_options):
            seen.append(copy.deepcopy(messages))
            if len(seen) == 2:
                self.assertIsInstance(messages[-1]["content"], list)
                self.assertEqual(messages[-2]["role"], "tool")
                self.assertEqual(messages[-2]["tool_call_id"], "image-1")
                return {"content": "image observed"}
            return {"tool_calls": [{"id": "image-1", "type": "function", "function": {"name": "request_image",
                    "arguments": json.dumps({"observation_id": f"obs-{client.observations}"})}}]}

        with patch("opencomputer.model_client._complete", side_effect=complete):
            self.assertEqual(run_task(client, "http://127.0.0.1:9/v1", "fake", "Read", max_steps=2,
                                      observation_mode="auto"), "Model response: image observed")
        self.assertNotIn("request_image", [name for name, _ in client.calls])
        self.assertEqual(sum(name == "get_semantic_state" for name, _ in client.calls), 3)
        self.assertEqual(sum(name == "get_window_state" for name, _ in client.calls), 3)

    def test_image_request_cannot_escape_semantic_mode_or_receipt_binding(self) -> None:
        for mode, arguments in (("semantic", {"observation_id": "obs-3"}),
                                ("auto", {"observation_id": "old"}),
                                ("auto", {"observation_id": "obs-3", "extra": True})):
            client = SemanticClient()
            message = {"tool_calls": [{"id": "image-1", "type": "function", "function": {"name": "request_image",
                       "arguments": json.dumps(arguments)}}]}
            with patch("opencomputer.model_client._complete", return_value=message):
                with self.assertRaises(RuntimeError):
                    run_task(client, "http://127.0.0.1:9/v1", "fake", "Read", observation_mode=mode)
            self.assertNotIn("get_window_state", [name for name, _ in client.calls])
            self.assertEqual(client.calls[-1][0], "stop")

    def test_tool_errors_are_mcp_errors(self) -> None:
        result = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bad"}}, FakeClient())
        self.assertTrue(result["result"]["isError"])
        self.assertIn("stale_observation", result["result"]["content"][0]["text"])

    def test_notifications_and_invalid_messages(self) -> None:
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, FakeClient()))
        self.assertEqual(handle_message([], FakeClient())["error"]["code"], -32600)
        self.assertEqual(handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": []}, FakeClient())["error"]["code"], -32602)
        self.assertEqual(handle_message({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": []}}, FakeClient())["result"]["protocolVersion"], "2024-11-05")

    def test_stdio_framing_parse_recovery_and_no_logs(self) -> None:
        source = io.StringIO('{broken}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        sink = io.StringIO()
        serve_stdio(FakeClient(), source, sink)
        rows = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["error"]["code"], -32700)
        self.assertEqual(rows[1]["result"], {})

    def test_client_rejects_remote_urls_and_header_injection(self) -> None:
        for url in ("http://example.com:80", "http://localhost:80", "https://127.0.0.1:80", "http://127.0.0.1:80/path", "http://user@127.0.0.1:80"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalClient(url, "token")
        with self.assertRaises(ValueError):
            LocalClient("http://127.0.0.1:80", "bad\r\nHeader")

    def test_runner_refreshes_between_actions_and_stops_at_limit(self) -> None:
        client = FakeClient()
        messages = [self.click_reply("obs-3"), self.click_reply("obs-7")]
        with patch("opencomputer.model_client._complete", side_effect=messages):
            with self.assertRaisesRegex(RuntimeError, "Step limit"):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click", 2)
        names = [name for name, _ in client.calls]
        self.assertEqual(names.count("click"), 2)
        self.assertEqual(names.count("get_window_state"), 8)
        self.assertEqual(names[-1], "stop")

    @staticmethod
    def click_reply(observation_id: str = "obs-3") -> dict:
        return {"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": "click", "arguments": json.dumps({"observation_id": observation_id, "x": 1, "y": 2})}}]}

    def test_runner_rejects_multiple_calls_and_always_stops(self) -> None:
        client = FakeClient()
        with patch("opencomputer.model_client._complete", return_value={"tool_calls": [{}, {}]}):
            with self.assertRaises(RuntimeError):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertEqual(client.calls[-1][0], "stop")

    def test_runner_stops_if_tool_discovery_fails(self) -> None:
        client = FakeClient()
        with patch.object(client, "tools", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertEqual(client.calls[-1][0], "stop")

    def test_runner_retains_only_latest_image_and_preserves_tool_history(self) -> None:
        client = FakeClient()
        captured = []

        def complete(endpoint, model, messages, tools, **options):
            captured.append(copy.deepcopy(messages))
            return self.click_reply() if len(captured) == 1 else {"content": "The button changed"}

        with patch("opencomputer.model_client._complete", side_effect=complete):
            result = run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertEqual(result, "Model response: The button changed")
        self.assertTrue(any(message["role"] == "tool" for message in captured[1]))
        for conversation in captured:
            images = [part for message in conversation if isinstance(message.get("content"), list)
                      for part in message["content"] if part["type"] == "image_url"]
            self.assertEqual(len(images), 1)

    def test_runner_rejects_bad_replies_before_dispatch(self) -> None:
        bad_argument = self.click_reply()
        bad_argument["tool_calls"][0]["function"]["arguments"] = "[]"
        unknown = self.click_reply()
        unknown["tool_calls"][0]["function"]["name"] = "press_key"
        malformed = self.click_reply()
        malformed["tool_calls"][0]["function"]["arguments"] = "{broken}"
        for reply in ({}, {"content": "<tool_call>click</tool_call>"}, {"tool_calls": {}},
                      {"refusal": "declined"}, self.click_reply("old"), bad_argument, unknown, malformed):
            client = FakeClient()
            with self.subTest(reply=reply), patch("opencomputer.model_client._complete", return_value=reply):
                with self.assertRaises(RuntimeError):
                    run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
            self.assertFalse(any(name == "click" for name, _ in client.calls))
            self.assertEqual(client.calls[-1][0], "stop")

    def test_disarm_while_model_generates_prevents_action(self) -> None:
        client = FakeClient()

        def complete(*args, **options):
            client.armed = False
            return self.click_reply()

        with patch("opencomputer.model_client._complete", side_effect=complete):
            with self.assertRaises(RunCancelled):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertFalse(any(name == "click" for name, _ in client.calls))

    def test_identical_refresh_rebinds_only_after_verifying_model_receipt(self) -> None:
        client = FakeClient()
        with patch("opencomputer.model_client._complete", side_effect=[self.click_reply(), {"content": "Observed changed button"}]):
            run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click", 2)
        click_arguments = next(arguments for name, arguments in client.calls if name == "click")
        self.assertEqual(click_arguments["observation_id"], "obs-4")

    def test_changed_pixels_or_geometry_reject_model_action(self) -> None:
        for change in ("pixels", "geometry"):
            client = FakeClient()

            def complete(*args, **options):
                if change == "pixels":
                    client.image = "different"
                else:
                    client.rect["width"] += 1
                return self.click_reply()

            with self.subTest(change=change), patch("opencomputer.model_client._complete", side_effect=complete):
                with self.assertRaisesRegex(RuntimeError, "Screen changed"):
                    run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
            self.assertFalse(any(name == "click" for name, _ in client.calls))
            self.assertEqual(client.calls[-1][0], "stop")

    def test_stop_then_rearm_same_window_does_not_resurrect_old_model_reply(self) -> None:
        client = FakeClient()

        def complete(*args, **options):
            client.generation += 2
            return self.click_reply()

        with patch("opencomputer.model_client._complete", side_effect=complete):
            with self.assertRaises(RunCancelled):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertFalse(any(name == "click" for name, _ in client.calls))

    def test_controller_restart_does_not_resurrect_old_model_reply(self) -> None:
        client = FakeClient()

        def complete(*args, **options):
            client.instance_id = "new-controller"
            return self.click_reply()

        with patch("opencomputer.model_client._complete", side_effect=complete):
            with self.assertRaises(RunCancelled):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertFalse(any(name == "click" for name, _ in client.calls))

    def test_planning_waits_for_partial_input_to_finish_painting(self) -> None:
        client = FakeClient()
        original_call = client.call
        captures = iter(["partial", "complete", "complete", "complete"])
        model_images = []

        def call(name, arguments=None):
            if name == "get_window_state":
                client.image = next(captures)
            return original_call(name, arguments)

        def complete(endpoint, model, messages, tools, **options):
            image = messages[-1]["content"][1]["image_url"]["url"]
            model_images.append(image)
            return {"content": "Observed completed input"}

        with patch.object(client, "call", side_effect=call), patch("opencomputer.model_client._complete", side_effect=complete):
            run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Inspect")
        self.assertEqual(model_images, ["data:image/png;base64,complete"])
        self.assertEqual(client.observations, 4)

    def test_unstable_screen_does_not_contact_model(self) -> None:
        client = FakeClient()
        original_call = client.call

        def call(name, arguments=None):
            if name == "get_window_state":
                client.image = str(client.observations)
            return original_call(name, arguments)

        with patch.object(client, "call", side_effect=call), patch("opencomputer.model_client._complete") as complete:
            with self.assertRaisesRegex(RuntimeError, "Observation budget expired"):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Inspect")
        complete.assert_not_called()
        self.assertEqual(client.calls[-1][0], "stop")

    def test_atomic_capture_uses_one_rpc_and_rejects_revoked_fields(self) -> None:
        client = FakeClient()
        authority = _check_authority(client)
        state = client.call("get_window_state")
        invalid_states = [
            {**state, "armed": False},
            {**state, "control_generation": state["control_generation"] + 1},
            {**state, "controller_instance_id": "replacement"},
            {**state, "window": {**state["window"], "id": "other"}},
            {**state, "window": {**state["window"], "pid": 8}},
        ]
        for invalid in invalid_states:
            with self.subTest(invalid=invalid), patch.object(client, "call", return_value=invalid) as call:
                with self.assertRaises(RunCancelled):
                    _read_observation(client, authority, None)
                call.assert_called_once_with("get_window_state")
        with patch.object(client, "call", return_value=state) as call:
            self.assertEqual(_read_observation(client, authority, None), state)
            call.assert_called_once_with("get_window_state")

    def test_atomic_capture_checks_cancellation_before_and_after_rpc(self) -> None:
        client = FakeClient()
        authority = _check_authority(client)
        state = client.call("get_window_state")
        cancel = threading.Event()
        cancel.set()
        with patch.object(client, "call", return_value=state) as call:
            with self.assertRaises(RunCancelled):
                _read_observation(client, authority, cancel)
            call.assert_not_called()
        cancel.clear()

        def cancelled_capture(name):
            cancel.set()
            return state

        with patch.object(client, "call", side_effect=cancelled_capture):
            with self.assertRaises(RunCancelled):
                _read_observation(client, authority, cancel)

    def test_observation_failure_reports_budget_not_pixel_instability(self) -> None:
        client = FakeClient()
        authority = _check_authority(client)
        events = []
        with patch("opencomputer.model_client.time.monotonic", side_effect=[0, 0, 1, 3]), \
                patch("opencomputer.model_client.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "2 captures, 2 consecutive matches"):
                _settled_observation(client, authority, None, events.append)
        self.assertEqual(events[0]["status"], "budget_exceeded")
        self.assertEqual(events[0]["captures"], 2)
        self.assertEqual(events[0]["consecutive_matches"], 2)
        self.assertEqual(events[0]["frame_changes"], 0)
        self.assertIn("capture_call_total_ms", events[0])

    def test_initial_status_failure_preserves_error_if_stop_also_fails(self) -> None:
        client = FakeClient()
        with patch.object(client, "call", side_effect=[OSError("original"), OSError("stop failed")]):
            with self.assertLogs("opencomputer.model_client", "WARNING"):
                with self.assertRaisesRegex(OSError, "original"):
                    run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")

    def test_malformed_rpc_ids_and_notifications(self) -> None:
        for message in ({}, {"jsonrpc": "2.0", "id": {}, "method": "ping"},
                        {"jsonrpc": "2.0", "id": True, "method": "ping"}):
            self.assertEqual(handle_message(message, FakeClient())["error"]["code"], -32600)

    def test_model_response_schema_rejects_missing_and_truncated_results(self) -> None:
        for payload in ([], {}, {"choices": []}, {"choices": [{"message": []}]},
                        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                _model_message(payload)

    def test_real_http_model_transport_is_bounded_and_nonstreaming(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "fixture response"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.wait_http_ready(server.server_port)
            reply = _complete(f"http://127.0.0.1:{server.server_port}/v1", "fake-model", [], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(reply["content"], "fixture response")
        self.assertEqual(requests[0][0], "/v1/chat/completions")
        self.assertFalse(requests[0][1]["stream"])
        self.assertEqual(requests[0][1]["max_tokens"], 1024)

    def test_transport_metrics_report_only_server_usage_and_sizes(self) -> None:
        events = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 150, "completion_tokens": 12, "total_tokens": 162,
                              "prompt_tokens_details": {"cached_tokens": 100},
                              "completion_tokens_details": {"reasoning_tokens": 4},
                              "secret": "must never appear in metrics"},
                    "timings": {"cache_n": 100, "prompt_n": 50, "prompt_ms": 25.5,
                                "predicted_n": 12, "predicted_ms": 200,
                                "prompt_per_second": 1960.8, "vision_secret": "must never appear in metrics"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.wait_http_ready(server.server_port)
            result = _complete(f"http://127.0.0.1:{server.server_port}/v1", "fake", [], [], metrics_callback=events.append)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(result, {"role": "assistant", "content": "ok"})
        self.assertEqual(events[0]["server_usage"], {"prompt_tokens": 150, "completion_tokens": 12,
                         "total_tokens": 162, "cached_tokens": 100, "reasoning_tokens": 4})
        self.assertEqual(events[0]["usage_source"], "server")
        self.assertEqual(events[0]["requested_reasoning"], "auto")
        self.assertEqual(events[0]["server_timings"], {"cache_n": 100, "prompt_n": 50, "prompt_ms": 25.5,
                         "predicted_n": 12, "predicted_ms": 200, "prompt_per_second": 1960.8})
        self.assertEqual(events[0]["timings_source"], "server")
        self.assertGreater(events[0]["request_bytes"], 0)
        self.assertNotIn("must never", json.dumps(events))

    def test_server_timings_reject_invalid_or_invented_counters(self) -> None:
        from opencomputer.model_client import _server_timings

        for payload in (None, {}, {"timings": []}):
            self.assertEqual(_server_timings(payload), {})
        self.assertEqual(_server_timings({"timings": {"cache_n": True, "prompt_n": 1.2,
                         "predicted_n": -1, "prompt_ms": float("inf"), "predicted_ms": float("nan"),
                         "prompt_per_second": "10", "predicted_per_second": -1,
                         "prompt_per_token_ms": None, "vision_encode_ms": 123}}), {})
        self.assertEqual(_server_timings({"timings": {"prompt_ms": 0, "prompt_n": 0,
                         "predicted_per_token_ms": 2.5}}),
                         {"prompt_n": 0, "prompt_ms": 0, "predicted_per_token_ms": 2.5})

    def test_cancel_interrupts_waiting_http_request(self) -> None:
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                entered.set()
                release.wait(4)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.wait_http_ready(server.server_port)

        def request():
            try:
                _complete(f"http://127.0.0.1:{server.server_port}/v1", "fake-model", [], [], cancel_event=cancel)
            except Exception as exc:
                errors.append(exc)

        caller = threading.Thread(target=request, daemon=True)
        caller.start()
        try:
            self.assertTrue(entered.wait(2))
            cancel.set()
            caller.join(2)
            self.assertFalse(caller.is_alive())
            self.assertIsInstance(errors[0], RunCancelled)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(2)

    def test_already_cancelled_transport_sends_no_request(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with patch("http.client.HTTPConnection.connect", side_effect=AssertionError("must not connect")):
            with self.assertRaises(RunCancelled):
                _complete("http://127.0.0.1:9/v1", "fake", [], [], cancel_event=cancel)


class ImageAndContextTests(unittest.TestCase):
    @staticmethod
    def state(width=101, height=53) -> dict:
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (width, height), "#102030").save(stream, format="PNG")
        return {"observation_id": "new", "image_base64": base64.b64encode(stream.getvalue()).decode(),
                "mime_type": "image/png", "rect": {"width": width, "height": height},
                "window": {"id": "owned", "pid": 7, "rect": {"width": width, "height": height}},
                "control_generation": 3, "controller_instance_id": "opaque-private-instance"}

    def test_odd_dimensions_downscale_without_mutating_original(self) -> None:
        state = self.state()
        original = copy.deepcopy(state)
        view = prepare_image_view(state, 64)
        self.assertEqual((view.image_width, view.image_height), (64, 33))
        self.assertEqual((view.source_width, view.source_height), (101, 53))
        self.assertEqual(state, original)
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(view.image_base64))) as image:
            self.assertEqual(image.size, (64, 33))
        arguments = view.native_arguments("drag", {"x": 0, "y": 0, "to_x": 63, "to_y": 32})
        self.assertEqual(arguments, {"x": 0, "y": 0, "to_x": 100, "to_y": 52})

    def test_all_coordinate_actions_map_pixel_centers_and_reject_invalid_points(self) -> None:
        view = ImageView("png", 50, 25, 100, 50)
        for action in ("click", "move", "scroll", "drag"):
            args = {"x": 49, "y": 24, **({"to_x": 0, "to_y": 0} if action == "drag" else {})}
            result = view.native_arguments(action, args)
            self.assertEqual((result["x"], result["y"]), (99, 49))
            for invalid in (True, 1.5, -1, 50):
                with self.subTest(action=action, invalid=invalid), self.assertRaises(RuntimeError):
                    view.native_arguments(action, {**args, "x": invalid})
        self.assertEqual(view.native_arguments("type_text", {"text": "hello"}), {"text": "hello"})

    def test_native_and_tiny_views_never_produce_zero_dimensions(self) -> None:
        state = self.state(101, 1)
        tiny = prepare_image_view(state, 1)
        self.assertEqual((tiny.image_width, tiny.image_height), (1, 1))
        native = prepare_image_view(state, 0)
        self.assertEqual(native.image_base64, state["image_base64"])
        self.assertEqual(native.native_arguments("click", {"x": 100, "y": 0}), {"x": 100, "y": 0})
        for width in (0, -1, True):
            invalid = {**state, "rect": {"width": width, "height": 1}}
            with self.assertRaises(RuntimeError):
                prepare_image_view(invalid)

    def test_compact_metadata_and_schemas_preserve_contract(self) -> None:
        state = self.state()
        efficient = _capture_message(state)
        text = efficient["content"][0]["text"].split(": ", 1)[1]
        self.assertEqual(json.loads(text), {"observation_id": "new", "image_width": 101, "image_height": 53})
        self.assertNotIn("opaque-private-instance", text)
        baseline = _capture_message(state, performance_profile="baseline")
        self.assertIn("opaque-private-instance", baseline["content"][0]["text"])
        definitions = FakeClient().tools()
        compact = build_model_tools(definitions)
        self.assertEqual(compact[0]["function"]["parameters"], definitions[0]["inputSchema"])
        body = build_model_request("model", [efficient], compact)
        self.assertEqual(json.loads(encode_model_request(body)), json.loads(encode_model_request(body, performance_profile="baseline")))
        self.assertLess(len(encode_model_request(body)), len(encode_model_request(body, performance_profile="baseline")))

    def test_reasoning_override_is_explicit_and_auto_preserves_wire_contract(self) -> None:
        baseline = {"model": "model", "messages": [], "tools": [], "tool_choice": "auto",
                    "parallel_tool_calls": False, "temperature": 0.1, "stream": False, "max_tokens": 1024}
        self.assertEqual(build_model_request("model", [], []), baseline)
        self.assertEqual(build_model_request("model", [], [], reasoning="auto"), baseline)
        self.assertEqual(build_model_request("model", [], [], reasoning="off"),
                         {**baseline, "chat_template_kwargs": {"enable_thinking": False}})
        for invalid in (None, "on", "low", {}, False):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_model_request("model", [], [], reasoning=invalid)

    def test_run_forwards_reasoning_off_without_changing_authority(self) -> None:
        client = FakeClient()
        with patch("opencomputer.model_client._complete", return_value={"content": "Observed fixture"}) as complete:
            run_task(client, "http://127.0.0.1:9/v1", "model", "Inspect", reasoning="off")
        self.assertEqual(complete.call_args.kwargs["reasoning"], "off")
        self.assertEqual(client.calls[-1][0], "stop")
        self.assertFalse(any(name == "click" for name, _ in client.calls))

    def test_history_budget_drops_whole_pairs_and_preserves_task_latest_action(self) -> None:
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "original task"}]
        events = []
        state = self.state()
        for index in range(7):
            _append_observation(messages, state, metrics_callback=events.append)
            call = {"id": f"call-{index}", "type": "function", "function": {"name": "type_text", "arguments": json.dumps({"text": "x" * 5000})}}
            messages.extend([{"role": "assistant", "tool_calls": [call], "content": ""},
                             {"role": "tool", "tool_call_id": call["id"], "content": "ok"}])
        _append_observation(messages, state, metrics_callback=events.append)
        self.assertEqual(messages[:2], [{"role": "system", "content": "rules"}, {"role": "user", "content": "original task"}])
        pairs = messages[2:-1]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["tool_calls"][0]["id"], "call-6")
        self.assertEqual(pairs[1]["tool_call_id"], "call-6")
        self.assertGreater(sum(event["dropped_action_groups"] for event in events), 0)

    def test_baseline_keeps_native_image_full_metadata_and_history(self) -> None:
        state = self.state()
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
        for index in range(6):
            _append_observation(messages, state, performance_profile="baseline")
            call = {"id": str(index), "type": "function", "function": {"name": "click", "arguments": "{}"}}
            messages.extend([{"role": "assistant", "tool_calls": [call]}, {"role": "tool", "tool_call_id": str(index), "content": "ok"}])
        _append_observation(messages, state, performance_profile="baseline")
        self.assertEqual(sum(message["role"] == "tool" for message in messages), 6)
        images = [part for message in messages if isinstance(message.get("content"), list)
                  for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["image_url"]["url"].endswith(state["image_base64"]))

    def test_cli_metrics_are_opt_in_and_written_on_failure_without_prompts(self) -> None:
        def fail(*args, **options):
            options["metrics_callback"]({"phase": "model", "elapsed_ms": 3, "server_usage": {}, "usage_source": "unavailable"})
            raise RuntimeError("fixture failure")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            stderr = io.StringIO()
            with patch("opencomputer.model_client.LocalClient.from_file", return_value=FakeClient()), \
                    patch("opencomputer.model_client.run_task", side_effect=fail), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    model_main(["private task must not enter metrics", "--model", "fake", "--metrics-json", str(path)])
            self.assertEqual(error.exception.code, 1)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["events"][0]["elapsed_ms"], 3)
            self.assertNotIn("private task", json.dumps(report))
            self.assertIn("fixture failure", stderr.getvalue())

    def test_cli_metrics_write_failure_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with patch("opencomputer.model_client.LocalClient.from_file", return_value=FakeClient()), \
                    patch("opencomputer.model_client.run_task", return_value="Model response: ok"), \
                    contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    model_main(["task", "--model", "fake", "--metrics-json", directory])
            self.assertEqual(error.exception.code, 1)
            self.assertIn("Could not write performance metrics", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
