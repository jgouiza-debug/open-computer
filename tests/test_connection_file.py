"""Real stdio/HTTP recovery tests using disposable fake services, never input."""

import io
import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import queue
import subprocess
import sys
import threading
import time
import unittest

from opencomputer.mcp import ClientError, ConnectionFileClient, LocalClient, handle_message, serve_stdio
from opencomputer.server import tool_definitions


class FakeService:
    def __init__(self, marker):
        self.marker, self.token = marker, "fake-token-" + marker
        self.calls, self.actions, self.status_failure = [], 0, None
        self.after_click = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, value, status=200, raw=None):
                data = json.dumps(value).encode() if raw is None else raw
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                if self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.reply({"error": {"code": "unauthorized", "message": "Unauthorized"}}, 401)
                    return False
                return True

            def do_GET(self):
                if self.path == "/fixture-ready":
                    self.reply({"ready": True})
                    return
                if self.authorized():
                    owner.calls.append("tools/list")
                    self.reply({"tools": tool_definitions()})

            def do_POST(self):
                if not self.authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = body["name"]
                owner.calls.append(name)
                if name == "status":
                    if owner.status_failure == "http":
                        self.reply({"error": {"code": "unavailable", "message": "Unavailable"}}, 503)
                    elif owner.status_failure == "json":
                        self.reply({}, raw=b"{")
                    elif owner.status_failure == "shape":
                        self.reply({"result": {"malformed": True}})
                    elif owner.status_failure == "semantic_format":
                        self.reply({"result": {"armed": False, "observation_kind": "semantic"}})
                    else:
                        self.reply({"result": {"armed": False, "selected_window": None, "marker": owner.marker}})
                else:
                    owner.actions += 1
                    if owner.after_click is not None:
                        owner.after_click()
                        self.reply({"error": {"code": "uncertain", "message": "Do not retry"}}, 503)
                    else:
                        self.reply({"result": {"ok": True}})

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.wait_ready()

    def wait_ready(self):
        deadline = time.monotonic() + 8
        last = None
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
            try:
                connection.request("GET", "/fixture-ready")
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return
            except (OSError, http.client.HTTPException) as error:
                last = error
            finally:
                connection.close()
            time.sleep(0.02)
        self.close()
        raise AssertionError("Fake HTTP service readiness failed") from last

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def publish(self, path):
        path.write_text(json.dumps({"url": self.url, "token": self.token}), encoding="utf-8")


class Steps:
    def __init__(self, steps):
        self.steps = iter(steps)

    def readline(self, _limit=-1):
        try:
            callback, message = next(self.steps)
        except StopIteration:
            return ""
        if callback:
            callback()
        return json.dumps(message) + "\n"


def tool(message_id, name):
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": {"name": name}}


class ConnectionFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "connection.json"
        self.first, self.second = FakeService("first"), FakeService("second")
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)

    def test_stdio_survives_missing_file_then_connects_and_blocks_rotated_action(self):
        bridge, sink = ConnectionFileClient(self.path), io.StringIO()
        steps = Steps([
            (None, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            (None, tool(2, "status")),
            (lambda: self.first.publish(self.path), tool(3, "status")),
            (lambda: self.second.publish(self.path), tool(4, "click")),
            (None, tool(5, "status")),
            (None, tool(6, "click")),
        ])
        serve_stdio(bridge, steps, sink)
        replies = [json.loads(line)["result"] for line in sink.getvalue().splitlines()]
        self.assertIn("serverInfo", replies[0])
        self.assertTrue(replies[1]["isError"])
        self.assertIn("connection_unavailable", replies[1]["content"][0]["text"])
        self.assertEqual(json.loads(replies[2]["content"][0]["text"])["marker"], "first")
        self.assertIn("connection_changed", replies[3]["content"][0]["text"])
        self.assertEqual(json.loads(replies[4]["content"][0]["text"])["marker"], "second")
        self.assertFalse(replies[5]["isError"])
        self.assertEqual(self.first.actions, 0)
        self.assertEqual(self.second.actions, 1)
        self.assertNotIn(self.first.token, sink.getvalue())
        self.assertNotIn(self.second.token, sink.getvalue())

    def test_real_stdio_process_remains_alive_across_missing_file_and_restart(self):
        root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-P", "-m", "opencomputer.mcp",
                                    "--connection", str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=self.directory.name,
            env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = queue.Queue()

        def read_stdout():
            for line in process.stdout:
                output.put(line)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()

        def exchange(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            return json.loads(output.get(timeout=8))["result"]

        try:
            self.assertIn("serverInfo", exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
            self.assertTrue(exchange(tool(2, "status"))["isError"])
            self.assertIsNone(process.poll())
            self.first.publish(self.path)
            self.assertFalse(exchange(tool(3, "status"))["isError"])
            self.second.publish(self.path)
            blocked = exchange(tool(4, "click"))
            self.assertIn("connection_changed", blocked["content"][0]["text"])
            self.assertEqual((self.first.actions, self.second.actions), (0, 0))
            recovered = exchange(tool(5, "status"))
            self.assertEqual(json.loads(recovered["content"][0]["text"])["marker"], "second")
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def test_initial_mutation_requires_successful_read_and_tools_list_can_establish_it(self):
        self.first.publish(self.path)
        bridge = ConnectionFileClient(self.path)
        with self.assertRaises(ClientError) as error:
            bridge.call("stop")
        self.assertEqual(error.exception.code, "connection_changed")
        self.assertEqual(self.first.calls, [])
        self.assertEqual(len(bridge.tools()), 13)
        bridge.call("select_window")
        self.assertEqual(self.first.actions, 1)

    def test_failed_read_invalidates_authority_and_does_not_enable_mutation(self):
        for failure in ("http", "json", "shape"):
            self.first.publish(self.path)
            bridge = ConnectionFileClient(self.path)
            bridge.tools()
            self.first.status_failure = failure
            with self.assertRaises(ClientError):
                bridge.call("status")
            before = list(self.first.calls)
            with self.assertRaises(ClientError) as error:
                bridge.call("click")
            self.assertEqual(error.exception.code, "connection_changed")
            self.assertEqual(self.first.calls, before)
        self.assertEqual(self.first.actions, 0)

    def test_outward_read_format_error_cannot_establish_or_retain_session(self):
        self.first.publish(self.path)
        for already_accepted in (False, True):
            bridge = ConnectionFileClient(self.path)
            if already_accepted:
                bridge.tools()
            self.first.status_failure = "semantic_format"
            read = handle_message(tool(1, "status"), bridge)
            self.assertTrue(read["result"]["isError"])
            self.assertIn("invalid_response", read["result"]["content"][0]["text"])
            calls_before_action = list(self.first.calls)
            action = handle_message(tool(2, "click"), bridge)
            self.assertTrue(action["result"]["isError"])
            self.assertIn("connection_changed", action["result"]["content"][0]["text"])
            self.assertEqual(self.first.calls, calls_before_action)
            self.assertEqual(self.first.actions, 0)

    def test_missing_or_malformed_then_restored_file_requires_new_read(self):
        for failure in ("missing", "malformed", "remote"):
            self.first.publish(self.path)
            bridge = ConnectionFileClient(self.path)
            bridge.call("status")
            if failure == "missing":
                self.path.unlink()
            elif failure == "malformed":
                self.path.write_text("{ secret must not appear", encoding="utf-8")
            else:
                self.path.write_text(json.dumps({"url": "http://example.com:80", "token": "secret"}), encoding="utf-8")
            with self.assertRaises(ClientError) as error:
                bridge.call("status")
            self.assertEqual(error.exception.code, "connection_unavailable")
            self.assertNotIn("secret", str(error.exception))
            self.first.publish(self.path)
            with self.assertRaises(ClientError) as error:
                bridge.call("click")
            self.assertEqual(error.exception.code, "connection_changed")
        self.assertEqual(self.first.actions, 0)

    def test_fixed_clients_do_not_adopt_connection_file_changes(self):
        self.first.publish(self.path)
        fixed = LocalClient(self.first.url, self.first.token)
        snapshot = LocalClient.from_file(self.path)
        self.second.publish(self.path)
        self.assertEqual(fixed.call("status")["marker"], "first")
        self.assertEqual(snapshot.call("status")["marker"], "first")

    def test_action_error_is_never_replayed_after_file_changes(self):
        self.first.publish(self.path)
        bridge = ConnectionFileClient(self.path)
        bridge.call("status")
        self.first.after_click = lambda: self.second.publish(self.path)
        with self.assertRaises(ClientError):
            bridge.call("click")
        self.assertEqual((self.first.actions, self.second.actions), (1, 0))
        self.assertEqual(bridge.call("status")["marker"], "second")
        self.assertEqual(self.second.actions, 0)

    def test_same_port_token_rotation_requires_a_read(self):
        self.first.publish(self.path)
        bridge = ConnectionFileClient(self.path)
        bridge.call("status")
        self.first.token = "rotated-fake-token"
        self.first.publish(self.path)
        with self.assertRaises(ClientError) as error:
            bridge.call("click")
        self.assertEqual(error.exception.code, "connection_changed")
        self.assertEqual(self.first.actions, 0)
        bridge.call("status")
        bridge.call("click")
        self.assertEqual(self.first.actions, 1)

    def test_formatting_is_not_session_change_but_atomic_replacement_is(self):
        self.first.publish(self.path)
        bridge = ConnectionFileClient(self.path)
        bridge.call("status")
        config = json.loads(self.path.read_text(encoding="utf-8"))
        self.path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        bridge.call("click")
        replacement = self.path.with_name("replacement.json")
        self.first.publish(replacement)
        replacement.replace(self.path)
        with self.assertRaises(ClientError):
            bridge.call("click")
        self.assertEqual(self.first.actions, 1)


if __name__ == "__main__":
    unittest.main()
