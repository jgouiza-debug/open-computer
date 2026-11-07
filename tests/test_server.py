"""Real HTTP tests for transport boundaries; no desktop input is generated."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from opencomputer.mcp import ClientError, LocalClient
from opencomputer.server import dispatch, start_server, ToolError


class FixtureController:
    def __init__(self) -> None:
        self.actions = []
        self.stopped = threading.Event()
        self.started = threading.Event()

    def status(self) -> dict:
        return {"armed": False, "stopped": self.stopped.is_set()}

    def list_windows(self) -> list:
        return [{"id": "fixture", "title": "Fixture"}]

    def select_window(self, window_id: str) -> dict:
        return {"selected": window_id}

    def get_window_state(self) -> dict:
        return {"observation_id": "fresh", "image_base64": "cG5n", "mime_type": "image/png"}

    def execute(self, action: str, observation_id: str, **params: object) -> dict:
        self.actions.append((action, observation_id, params))
        if params.get("text") == "block until stopped":
            self.started.set()
            if not self.stopped.wait(5):
                raise RuntimeError("stop did not interrupt")
        return {"ok": True}

    def stop(self) -> dict:
        self.stopped.set()
        return self.status()


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FixtureController()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "connection.json"
        self.server = start_server(self.controller, port=0, connection_path=self.path)
        self.client = LocalClient.from_file(self.path)

    def tearDown(self) -> None:
        self.server.close()
        self.temp.cleanup()

    def raw_request(self, headers: dict, path: str = "/health", method: str = "GET", body: str | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.client.port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def test_authentication_origin_and_dns_rebinding(self) -> None:
        self.assertEqual(self.raw_request({}), 401)
        auth = {"Authorization": f"Bearer {self.server.token}"}
        self.assertEqual(self.raw_request(auth), 200)
        self.assertEqual(self.raw_request({**auth, "Origin": "http://127.0.0.1"}), 403)
        self.assertEqual(self.raw_request({**auth, "Origin": "null"}), 403)
        self.assertEqual(self.raw_request({**auth, "Host": "attacker.example"}), 403)
        self.assertEqual(self.raw_request({"Authorization": "Bearer wrong"}), 401)
        self.assertEqual(self.raw_request({"Authorization": "Bearer é"}), 401)

    def test_no_remote_arm_or_unlisted_parameters(self) -> None:
        for name in ("arm", "local_arm", "reset_stop", "__class__"):
            with self.subTest(name=name), self.assertRaises(ClientError):
                self.client.call(name)
        with self.assertRaises(ClientError):
            self.client.call("click", {"observation_id": "fresh", "x": 1, "y": 2, "shell": "anything"})
        self.assertEqual(self.controller.actions, [])

    def test_types_limits_and_nonfinite_json_rejected(self) -> None:
        for arguments in (
            {"observation_id": "fresh", "x": True, "y": 2},
            {"observation_id": "fresh", "x": 1.5, "y": 2},
            {"observation_id": "fresh", "x": 1, "y": 2, "count": 3},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ClientError):
                self.client.call("click", arguments)
        auth = {"Authorization": f"Bearer {self.server.token}", "Content-Type": "application/json"}
        self.assertEqual(self.raw_request(auth, "/tools/call", "POST", '{"name":"drag","arguments":{"duration":NaN}}'), 400)
        self.assertEqual(self.controller.actions, [])

    def test_authenticated_dispatch_and_screenshot(self) -> None:
        self.assertEqual(self.client.call("list_windows")[0]["id"], "fixture")
        self.assertEqual(self.client.call("get_window_state")["image_base64"], "cG5n")
        self.client.call("click", {"observation_id": "fresh", "x": 1, "y": 2})
        self.assertEqual(self.controller.actions, [("click", "fresh", {"x": 1, "y": 2})])

    def test_stop_not_queued_behind_an_action(self) -> None:
        errors = []

        def action() -> None:
            try:
                self.client.call("type_text", {"observation_id": "fresh", "text": "block until stopped"})
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=action)
        thread.start()
        self.assertTrue(self.controller.started.wait(3), repr(errors))
        self.client.call("stop")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_close_removes_own_credentials_only(self) -> None:
        self.path.write_text(json.dumps({"token": "another launch"}), encoding="utf-8")
        self.server.close()
        self.assertTrue(self.path.exists())

    def test_reject_non_loopback_bind(self) -> None:
        with self.assertRaises(ValueError):
            start_server(self.controller, host="0.0.0.0", port=0)

    def test_dispatch_rejects_non_object(self) -> None:
        with self.assertRaises(ToolError):
            dispatch(self.controller, "click", [])


if __name__ == "__main__":
    unittest.main()
