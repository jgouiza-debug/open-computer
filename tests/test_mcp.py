"""MCP protocol and local model boundary tests with deterministic fixtures."""

import io
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from opencomputer.mcp import ClientError, LocalClient, content_blocks, handle_message, serve_stdio
from opencomputer.model_client import _complete, _model_message, _check_authority, _settled_observation, run_task, RunCancelled


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
            return {"observation_id": f"obs-{self.observations}", "control_generation": self.generation, "controller_instance_id": self.instance_id, "window": {"id": "fixture", "pid": 7, "rect": dict(self.rect)}, "image_base64": self.image, "mime_type": "image/png"}
        if name == "bad":
            raise ClientError("stale_observation", "Observe again")
        return {"ok": True}


class McpTests(unittest.TestCase):
    @staticmethod
    def wait_http_ready(port: int) -> None:
        # A completed harmless request proves the listening thread is servicing
        # connections before a cancellation timing assertion starts.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/fixture-ready")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()

    def test_initialize_tools_and_image_blocks(self) -> None:
        client = FakeClient()
        result = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, client)
        self.assertEqual(result["result"]["protocolVersion"], "2025-03-26")
        result = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_window_state"}}, client)
        blocks = result["result"]["content"]
        self.assertEqual(blocks[1], {"type": "image", "data": "cG5n", "mimeType": "image/png"})
        self.assertNotIn("image_base64", blocks[0]["text"])

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

        def complete(endpoint, model, messages, tools):
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

        def complete(*args):
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

            def complete(*args):
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

        def complete(*args):
            client.generation += 2
            return self.click_reply()

        with patch("opencomputer.model_client._complete", side_effect=complete):
            with self.assertRaises(RunCancelled):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click")
        self.assertFalse(any(name == "click" for name, _ in client.calls))

    def test_controller_restart_does_not_resurrect_old_model_reply(self) -> None:
        client = FakeClient()

        def complete(*args):
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

        def complete(endpoint, model, messages, tools):
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
            with self.assertRaisesRegex(RuntimeError, "did not settle"):
                run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Inspect")
        complete.assert_not_called()
        self.assertEqual(client.calls[-1][0], "stop")

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


if __name__ == "__main__":
    unittest.main()
