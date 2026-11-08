"""Task-bound headless contracts using fake input only; no native/UI/model calls."""

import base64
from contextlib import redirect_stdout
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from opencomputer.backend import FakeBackend
from opencomputer.mcp import ClientError
from opencomputer.headless import HeadlessSession, discover_windows, serve_stdio


class FakeHotkey:
    def __init__(self, callback):
        self.callback = callback
        self.closed = False

    def close(self):
        self.closed = True


class Backend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.window["pid"] = 42
        self.process_start = "123456789012345678"
        self.pixels = b"fixed-test-frame"

    def process_identity(self, window_id):
        window = self.get_window(window_id)
        return {"id": window["id"], "pid": window["pid"], "process_start": self.process_start}

    def capture(self, window_id):
        return {"image_base64": base64.b64encode(self.pixels).decode(), "mime_type": "image/png",
                "rect": self.get_window(window_id)["rect"]}


class HeadlessTests(unittest.TestCase):
    def session(self, **options):
        backend = options.pop("backend", Backend())
        session = HeadlessSession(backend, window_id="demo", pid=42,
            process_start=options.pop("process_start", backend.process_start),
            hotkey_factory=FakeHotkey, **options)
        self.addCleanup(session.close)
        return session, backend

    def error(self, code, callback):
        with self.assertRaises(ClientError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_discovery_reads_identity_without_capture_or_input(self):
        backend = Backend()
        with patch.object(backend, "capture", side_effect=AssertionError("No screenshots")):
            windows = discover_windows(backend)
        self.assertEqual(windows[0]["process_start"], backend.process_start)
        self.assertFalse(backend.actions)

    def test_input_requires_process_start_and_bounds(self):
        for options in ({"process_start": None, "allow_input": True}, {"ttl": 0},
                        {"ttl": 3601}, {"max_actions": 257}, {"max_actions": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.session(**options)

    def test_read_only_default_and_no_remote_grant_or_target_change(self):
        session, backend = self.session()
        self.assertFalse(session.call("status")["armed"])
        observation = session.call("observe")
        self.assertEqual(observation["observation_kind"], "image")
        self.error("not_armed", lambda: session.call("click", {"observation_id": observation["observation_id"], "x": 1, "y": 1}))
        for name in ("arm", "reset_stop", "select_window", "list_windows"):
            self.error("unknown_tool", lambda: session.call(name, {"window_id": "other"}))
        self.assertFalse(backend.actions)

    def test_exact_process_identity_and_pixel_freshness_before_input(self):
        session, backend = self.session(allow_input=True)
        receipt = session.call("get_window_state")
        backend.pixels = b"changed-frame"
        self.error("observation_changed", lambda: session.call("click", {"observation_id": receipt["observation_id"], "x": 1, "y": 1}))
        self.assertFalse(backend.actions)
        receipt = session.call("get_window_state")
        backend.process_start = "different-process"
        self.error("target_changed", lambda: session.call("click", {"observation_id": receipt["observation_id"], "x": 1, "y": 1}))
        self.assertFalse(backend.actions)
        self.assertTrue(session.controller.status()["stopped"])

    def test_last_budget_action_finishes_then_stop_is_latched(self):
        session, backend = self.session(allow_input=True, max_actions=1)
        receipt = session.call("observe")
        session.call("click", {"observation_id": receipt["observation_id"], "x": 1, "y": 1})
        self.assertEqual(len(backend.actions), 1)
        self.assertTrue(session.call("status")["stopped"])
        self.error("grant_revoked", lambda: session.call("type_text", {"observation_id": receipt["observation_id"], "text": "no"}))
        self.assertEqual(len(backend.actions), 1)

    def test_expiry_rechecked_after_capture_before_input(self):
        clock = [10.0]
        session, backend = self.session(allow_input=True, ttl=1, clock=lambda: clock[0])
        receipt = session.call("get_window_state")
        capture = backend.capture
        def expires(window_id):
            result = capture(window_id)
            clock[0] = 12.0
            return result
        with patch.object(backend, "capture", side_effect=expires):
            self.error("grant_expired", lambda: session.call("click", {"observation_id": receipt["observation_id"], "x": 1, "y": 1}))
        self.assertFalse(backend.actions)

    def test_auto_fallback_only_for_expected_semantic_inadequacy(self):
        from opencomputer.controller import ControllerError
        session, _ = self.session()
        for code in ("semantic_unavailable", "semantic_empty", "semantic_incomplete"):
            with patch.object(session.controller, "get_semantic_state", side_effect=ControllerError(code, "test")):
                self.assertEqual(session.call("observe")["observation_kind"], "image")
        for code in ("cancelled", "target_changed", "observation_changed", "semantic_provider_failed"):
            with patch.object(session.controller, "get_semantic_state", side_effect=ControllerError(code, "test")), \
                    patch.object(session.controller, "get_window_state", side_effect=AssertionError("No fallback")):
                self.error(code, lambda: session.call("observe"))

    def test_stdio_eof_cancels_a_blocked_observation(self):
        session, backend = self.session(allow_input=True)
        entered = threading.Event()
        original_capture = backend.capture
        def blocked(window_id):
            entered.set()
            if not backend.cancel_event.wait(2):
                raise AssertionError("EOF did not cancel outside operation lock")
            return original_capture(window_id)
        class Reader:
            count = 0
            def readline(self, _limit):
                self.count += 1
                if self.count == 1:
                    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "observe"}}) + "\n"
                if not entered.wait(1):
                    raise AssertionError("Worker did not enter observation")
                return ""
        with patch.object(backend, "capture", side_effect=blocked):
            started = time.monotonic()
            serve_stdio(session, source=Reader(), sink=io.StringIO())
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(session.controller.status()["stopped"])

    def test_stdio_valid_stop_interrupts_before_eof_but_malformed_stop_does_not(self):
        for arguments, valid in (({}, True), ({"unexpected": True}, False)):
            with self.subTest(arguments=arguments):
                session, backend = self.session(allow_input=True)
                entered = threading.Event()
                original = backend.capture
                seen = []
                def blocked(window_id):
                    entered.set()
                    backend.cancel_event.wait(2)
                    return original(window_id)
                class Reader:
                    count = 0
                    def readline(self, _limit):
                        self.count += 1
                        if self.count == 1:
                            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "observe"}}
                        elif self.count == 2:
                            entered.wait(1)
                            message = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "stop", "arguments": arguments}}
                        else:
                            seen.append(backend.cancel_event.wait(0.2))
                            return ""
                        return json.dumps(message) + "\n"
                with patch.object(backend, "capture", side_effect=blocked):
                    serve_stdio(session, source=Reader(), sink=io.StringIO())
                self.assertEqual(seen, [valid])

    def test_bad_argument_types_do_not_escape_protocol_errors(self):
        session, _ = self.session()
        self.error("invalid_arguments", lambda: session.call("observe", {"mode": []}))
        self.error("invalid_arguments", lambda: session.call("status", []))

    def test_direct_run_forwards_explicit_bounded_options_without_network_or_input(self):
        from opencomputer import headless
        session, backend = self.session(allow_input=True)
        output = io.StringIO()
        with patch.object(headless, "HeadlessSession", return_value=session) as constructor, \
                patch("opencomputer.model_client.run_task", return_value="Stopped") as runner, \
                redirect_stdout(output):
            result = headless.main(["run", "--window-id", "demo", "--pid", "42", "--process-start", backend.process_start,
                "--allow-input", "--endpoint", "http://127.0.0.1:8111/v1", "--model", "existing", "--task", "Disposable test"])
        self.assertEqual(result, 0)
        self.assertTrue(constructor.call_args.kwargs["allow_input"])
        self.assertEqual(runner.call_args.kwargs["max_output_tokens"], 256)
        self.assertEqual(runner.call_args.kwargs["observation_mode"], "auto")
        self.assertEqual(runner.call_args.kwargs["max_steps"], 8)
        self.assertTrue(json.loads(output.getvalue())["stopped"])
        self.assertFalse(json.loads(output.getvalue())["task_completion_verified"])
        self.assertFalse(backend.actions)


class SafetyTests(unittest.TestCase):
    def test_hotkey_ready_and_held_chord_stops_once_without_native_calls(self):
        from opencomputer.safety import SafetyHotkey
        calls = []
        watcher = SafetyHotkey(lambda: calls.append("stop"), poll=lambda: True)
        watcher.close()
        self.assertEqual(calls, ["stop"])
        self.assertFalse(watcher.thread.is_alive())

    def test_hotkey_reader_failure_stops_independently(self):
        from opencomputer.safety import SafetyHotkey
        stopped = threading.Event()
        count = [0]
        def poll():
            count[0] += 1
            if count[0] > 1:
                raise RuntimeError("reader failed")
            return False
        watcher = SafetyHotkey(stopped.set, poll=poll)
        self.addCleanup(watcher.close)
        self.assertTrue(stopped.wait(1))
        self.assertTrue(watcher.failed)

    def test_hotkey_startup_failure_rejects_authorization(self):
        from opencomputer.safety import SafetyHotkey
        with self.assertRaises(RuntimeError):
            SafetyHotkey(lambda: None, poll=lambda: (_ for _ in ()).throw(RuntimeError("failed")))


if __name__ == "__main__":
    unittest.main()
