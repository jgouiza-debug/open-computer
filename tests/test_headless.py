"""Task-bound headless contracts using fake input only; no native/UI/model calls."""

import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType
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


class FakeOutline:
    """Pure state machine, never creates a window or invokes native APIs."""

    def __init__(self):
        self.trace = []
        self.events = []
        self.visible = False
        self.hidden = False
        self.closed = False
        self.failed = False
        self.rect = None

    def check_health(self):
        if self.failed or self.closed:
            raise RuntimeError("Fake feedback failure")

    def emit(self, event):
        self.trace.append(event["type"])
        self.events.append(event)
        if not self.hidden and event["type"] in {"target", "armed"}:
            self.visible = True

    def wait_visible(self, timeout=2):
        self.trace.append("visible_ack")
        if not self.visible:
            raise RuntimeError("No visible frame")

    def update_target(self, rect):
        self.rect = rect

    def hide(self):
        self.trace.append("hide")
        self.hidden = True
        self.visible = False

    def close(self):
        self.hide()
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

    def test_feedback_ack_precedes_arm_and_stop_hide_precedes_event(self):
        outline = FakeOutline()
        session, _ = self.session(allow_input=True, outline_factory=lambda: outline)
        self.assertLess(outline.trace.index("target"), outline.trace.index("visible_ack"))
        self.assertLess(outline.trace.index("visible_ack"), outline.trace.index("armed"))
        self.assertEqual(session.call("status")["feedback"], {"enabled": True, "healthy": True})
        session.stop()
        self.assertLess(outline.trace.index("hide"), outline.trace.index("stopped"))
        outline.emit({"type": "armed"})
        self.assertFalse(outline.visible)
        self.assertTrue(session.controller.status()["stopped"])

    def test_read_only_session_also_shows_feedback_but_fake_default_has_none(self):
        outline = FakeOutline()
        session, _ = self.session(outline_factory=lambda: outline)
        self.assertTrue(outline.visible)
        self.assertFalse(session.controller.status()["armed"])
        without, _ = self.session()
        self.assertEqual(without.call("status")["feedback"], {"enabled": False, "healthy": False})

    def test_failed_visible_ack_never_arms_and_closes_partial_resources(self):
        outline, backend = FakeOutline(), Backend()
        def fail(**_):
            raise RuntimeError("No visible frame")
        outline.wait_visible = fail
        with self.assertRaisesRegex(RuntimeError, "No visible frame"):
            HeadlessSession(backend, window_id="demo", pid=42, process_start=backend.process_start,
                allow_input=True, hotkey_factory=FakeHotkey, outline_factory=lambda: outline)
        self.assertNotIn("armed", outline.trace)
        self.assertTrue(outline.closed)
        self.assertTrue(backend.cancel_event.is_set())
        self.assertFalse(backend.actions)

    def test_feedback_failure_at_authority_boundary_prevents_input(self):
        outline = FakeOutline()
        session, backend = self.session(allow_input=True, outline_factory=lambda: outline)
        receipt = session.call("get_window_state")
        outline.failed = True
        self.error("overlay_error", lambda: session.call("click", {"observation_id": receipt["observation_id"], "x": 1, "y": 1}))
        self.assertFalse(backend.actions)
        self.assertTrue(backend.cancel_event.is_set())
        self.assertTrue(outline.hidden)
        self.assertEqual(session.call("status")["feedback"], {"enabled": True, "healthy": False})

    def test_watchdog_updates_geometry_and_revokes_lost_identity(self):
        outline = FakeOutline()
        session, backend = self.session(allow_input=True, outline_factory=lambda: outline)
        backend.window["rect"]["x"] = -50
        deadline = time.monotonic() + 1
        while (outline.rect is None or outline.rect["x"] != -50) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(outline.rect)
        self.assertEqual(outline.rect["x"], -50)
        backend.process_start = "replacement"
        self.assertTrue(backend.cancel_event.wait(1))
        self.assertTrue(outline.hidden)
        self.assertTrue(session.controller.status()["stopped"])

    def test_close_reaps_feedback_even_if_controller_close_fails(self):
        outline = FakeOutline()
        session, _ = self.session(allow_input=True, outline_factory=lambda: outline)
        with patch.object(session.controller, "close", side_effect=RuntimeError("Provider close failed")):
            with self.assertRaisesRegex(RuntimeError, "Provider close failed"):
                session.close()
        self.assertTrue(outline.closed)
        self.assertTrue(outline.hidden)

    def test_controller_feedback_bridge_omits_titles_and_input_text(self):
        outline = FakeOutline()
        session, backend = self.session(allow_input=True, outline_factory=lambda: outline)
        session.controller._emit("action", selected_window=backend.window,
            action="type_text", text="PRIVATE_INPUT", x=123, y=456)
        event = outline.events[-1]
        self.assertNotIn("text", event)
        self.assertNotIn("title", event["selected_window"])
        self.assertEqual((event["x"], event["y"]), (123, 456))

    def test_controller_disarm_event_ends_the_headless_grant(self):
        outline = FakeOutline()
        session, backend = self.session(allow_input=True, outline_factory=lambda: outline)
        session.controller.disarm()
        self.error("grant_revoked", lambda: session.call("type_text", {"observation_id": "old", "text": "no"}))
        self.assertFalse(backend.actions)

    def test_stop_cancels_input_and_semantics_before_blocking_feedback_hide(self):
        outline = FakeOutline()
        session, backend = self.session(allow_input=True, outline_factory=lambda: outline)
        entered, release, semantic_cancel = threading.Event(), threading.Event(), threading.Event()
        session.controller._semantic_cancel = semantic_cancel
        original_hide = outline.hide
        def blocking_hide():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Test did not release feedback")
            original_hide()
        outline.hide = blocking_hide
        results = []
        worker = threading.Thread(target=lambda: results.append(session.stop()))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(backend.cancel_event.is_set(), "Input cancellation waited for feedback")
            self.assertTrue(semantic_cancel.is_set(), "Semantic cancellation waited for feedback")
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["stopped"])
        session.close()
        self.assertTrue(outline.closed)

    def test_event_only_cancel_does_not_wait_for_controller_authority_lock(self):
        session, backend = self.session(allow_input=True)
        done, semantic_cancel = threading.Event(), threading.Event()
        session.controller._semantic_cancel = semantic_cancel
        def cancel():
            session.controller.request_cancel()
            done.set()
        with session.controller._lock:
            worker = threading.Thread(target=cancel)
            worker.start()
            completed = done.wait(0.5)
        worker.join(timeout=1)
        self.assertTrue(completed)
        self.assertTrue(backend.cancel_event.is_set())
        self.assertTrue(semantic_cancel.is_set())

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


class NativeProviderPinTests(unittest.TestCase):
    def module(self, value):
        module = ModuleType("opencomputer._native_pin")
        module.NATIVE_SHA256 = value
        return module

    def test_source_default_never_requires_generated_pin_or_starts_helper(self):
        from opencomputer import headless
        with patch.object(headless.sys, "frozen", False, create=True), \
                patch.dict(sys.modules, {"opencomputer._native_pin": None}), \
                patch.object(headless, "NativeSemanticProvider") as provider:
            self.assertIsNone(headless._native_provider(None))
        provider.assert_not_called()

    def test_frozen_default_requires_matching_generated_build_pin(self):
        from opencomputer import headless
        with tempfile.TemporaryDirectory(prefix="portable pin café ") as temporary:
            root = Path(temporary)
            helper = root / "optional/opencomputer-native.exe"
            helper.parent.mkdir()
            helper.write_bytes(b"current CI-built helper fixture")
            digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            with patch.object(headless.sys, "frozen", True, create=True), \
                    patch.object(headless.sys, "executable", str(root / "OpenComputerCLI.exe")), \
                    patch.dict(sys.modules, {"opencomputer._native_pin": self.module(digest)}), \
                    patch.object(headless, "NativeSemanticProvider", return_value="provider") as provider:
                self.assertEqual(headless._native_provider(None), "provider")
                provider.assert_called_once_with(helper.resolve())

    def test_frozen_missing_or_malformed_pin_never_falls_back(self):
        from opencomputer import headless
        missing_attribute = ModuleType("opencomputer._native_pin")
        modules = [None, missing_attribute, *[self.module(value) for value in
            (None, True, 123, "", "a" * 63, "A" * 64, "z" * 64, "a" * 64 + "\n")]]
        for module in modules:
            with self.subTest(module=module), \
                    patch.object(headless.sys, "frozen", True, create=True), \
                    patch.dict(sys.modules, {"opencomputer._native_pin": module}), \
                    patch.object(headless, "NativeSemanticProvider") as provider:
                with self.assertRaises(ValueError):
                    headless._native_provider(None)
                provider.assert_not_called()

    def test_frozen_missing_or_changed_helper_is_rejected_before_provider_creation(self):
        from opencomputer import headless
        with tempfile.TemporaryDirectory(prefix="portable helper ") as temporary:
            root = Path(temporary)
            helper = root / "optional/opencomputer-native.exe"
            helper.parent.mkdir()
            with patch.object(headless.sys, "frozen", True, create=True), \
                    patch.object(headless.sys, "executable", str(root / "OpenComputerCLI.exe")), \
                    patch.dict(sys.modules, {"opencomputer._native_pin": self.module("0" * 64)}), \
                    patch.object(headless, "NativeSemanticProvider") as provider:
                for present in (False, True):
                    if present:
                        helper.write_bytes(b"different helper")
                    with self.subTest(present=present), self.assertRaises(ValueError):
                        headless._native_provider(None)
                provider.assert_not_called()

    def test_explicit_helper_choice_bypasses_build_pin_even_when_frozen(self):
        from opencomputer import headless
        with tempfile.TemporaryDirectory(prefix="explicit helper ") as temporary:
            helper = Path(temporary) / "chosen.exe"
            helper.write_bytes(b"user-selected helper fixture")
            with patch.object(headless.sys, "frozen", True, create=True), \
                    patch.dict(sys.modules, {"opencomputer._native_pin": None}), \
                    patch.object(headless, "NativeSemanticProvider", return_value="chosen") as provider:
                self.assertEqual(headless._native_provider(helper), "chosen")
                provider.assert_called_once_with(helper.resolve())


if __name__ == "__main__":
    unittest.main()
