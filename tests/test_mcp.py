"""MCP protocol and local model boundary tests with deterministic fixtures."""

import io
import json
import unittest
from unittest.mock import patch

from opencomputer.mcp import ClientError, LocalClient, content_blocks, handle_message, serve_stdio
from opencomputer.model_client import run_task


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def tools(self) -> list:
        return [{"name": "click", "description": "Click", "inputSchema": {"type": "object"}}]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        if name == "status":
            return {"armed": True, "selected_window": {"id": "fixture"}}
        if name == "get_window_state":
            return {"observation_id": str(len(self.calls)), "image_base64": "cG5n", "mime_type": "image/png"}
        if name == "bad":
            raise ClientError("stale_observation", "Observe again")
        return {"ok": True}


class McpTests(unittest.TestCase):
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
        message = {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "click", "arguments": '{"observation_id":"2","x":1,"y":2}'}}]}
        with patch("opencomputer.model_client._complete", return_value=message):
            result = run_task(client, "http://127.0.0.1:8080/v1", "fixture", "Click", 2)
        self.assertIn("Step limit", result)
        self.assertEqual([name for name, _ in client.calls], ["status", "get_window_state", "click", "get_window_state", "click", "stop"])

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


if __name__ == "__main__":
    unittest.main()
