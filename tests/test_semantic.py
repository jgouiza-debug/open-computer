"""Semantic authority tests use fake windows/providers only; no native calls."""

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from opencomputer.backend import FakeBackend
from opencomputer.controller import Controller, ControllerError
from opencomputer.native_client import NativeError
from opencomputer.semantic import (NativeSemanticProvider, SemanticError, compact_semantic,
                                   semantic_fingerprint, semantic_projection, validate_semantic)


def snapshot(window):
    rect = {key: window["rect"][key] for key in ("x", "y", "width", "height")}
    root = {"id": "e0", "parent": None, "role": "window", "name": "Fixture",
            "bounds": {"x": 0, "y": 0, "width": rect["width"], "height": rect["height"]},
            "enabled": True, "focusable": False, "password": False}
    button = {"id": "e1", "parent": "e0", "role": "button", "name": "Test click",
              "bounds": {"x": 10, "y": 20, "width": 100, "height": 40},
              "enabled": True, "focusable": True, "password": False}
    return {"snapshot_id": "opaque-1", "window": {"id": window["id"], "pid": window["pid"],
            "title": window["title"], "rect": rect}, "nodes": [root, button],
            "truncated": False, "truncation_reasons": [], "metrics": {"inspect_ms": 1}}


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.mutate = None
        self.closed = False

    def inspect(self, window, *, cancel_event):
        self.calls += 1
        result = snapshot(window)
        if self.mutate:
            self.mutate(result, cancel_event)
        return result

    def status(self):
        return {"configured": True, "state": "closed" if self.closed else "ready"}

    def close(self):
        self.closed = True


class SemanticControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.backend.window.update(id="17", pid=42, title="Fixture")
        self.provider = FakeProvider()
        self.controller = Controller(self.backend, semantic_provider=self.provider)
        self.controller.select_window("17")
        self.controller.arm()
        self.addCleanup(self.controller.close)

    def assert_code(self, code, callback):
        with self.assertRaises(ControllerError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_semantic_receipt_revalidates_then_executes_once(self):
        state = self.controller.get_semantic_state()
        self.assertEqual(state["observation_kind"], "semantic")
        self.assertIn("image_base64", state)
        self.assertEqual(len(state["semantic"]["columns"]), 12)
        self.controller.execute("click", state["observation_id"], x=20, y=30)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(len(self.backend.actions), 1)
        self.assert_code("stale_observation", lambda: self.controller.execute("click", state["observation_id"], x=20, y=30))

    def test_changed_semantics_with_identical_pixels_rejects_and_consumes(self):
        state = self.controller.get_semantic_state()
        self.provider.mutate = lambda data, event: data["nodes"][1].update(name="Delete all")
        self.assert_code("semantic_changed", lambda: self.controller.execute("click", state["observation_id"], x=20, y=30))
        self.assertFalse(self.backend.actions)
        self.assertIsNone(self.controller.status()["observation_id"])

    def test_pixel_change_during_observation_or_before_execute_rejects(self):
        capture = self.backend.capture
        calls = 0

        def changing(window_id):
            nonlocal calls
            calls += 1
            data = capture(window_id)
            if calls == 2:
                data["image_base64"] += "different"
            return data

        with patch.object(self.backend, "capture", side_effect=changing):
            self.assert_code("observation_changed", self.controller.get_semantic_state)
        state = self.controller.get_semantic_state()
        with patch.object(self.backend, "capture", side_effect=lambda wid: {**capture(wid), "image_base64": "changed"}):
            self.assert_code("semantic_changed", lambda: self.controller.execute("click", state["observation_id"], x=20, y=30))
        self.assertFalse(self.backend.actions)

    def test_incomplete_empty_and_wrong_identity_never_publish_receipt(self):
        cases = [("semantic_incomplete", lambda d, e: d.update(truncated=True, truncation_reasons=["node_limit"])),
                 ("semantic_empty", lambda d, e: d.update(nodes=d["nodes"][:1])),
                 ("target_changed", lambda d, e: d["window"].update(pid=123))]
        for code, mutate in cases:
            with self.subTest(code=code):
                self.provider.mutate = mutate
                self.assert_code(code, self.controller.get_semantic_state)
                self.assertIsNone(self.controller.status()["observation_id"])

    def test_authority_change_during_read_is_rejected(self):
        self.provider.mutate = lambda d, e: self.controller.disarm()
        self.assert_code("cancelled", self.controller.get_semantic_state)
        self.assertFalse(self.backend.actions)

    def test_cancel_wins_over_unavailable_and_cancels_execute_revalidation(self):
        state = self.controller.get_semantic_state()

        def cancel_then_unavailable(data, event):
            self.controller.disarm()
            raise SemanticError("semantic_unavailable", "Not an eligible fallback after cancellation")

        self.provider.mutate = cancel_then_unavailable
        self.assert_code("cancelled", lambda: self.controller.execute("click", state["observation_id"], x=20, y=30))
        self.assertFalse(self.backend.actions)
        self.assertIsNone(self.controller.status()["observation_id"])

    def test_stop_disarm_select_and_close_interrupt_blocked_reads(self):
        for operation in ("stop", "disarm", "select_window", "close"):
            with self.subTest(operation=operation):
                provider = FakeProvider()
                controller = Controller(self.backend, semantic_provider=provider)
                controller.select_window("17")
                entered = threading.Event()
                errors = []

                def blocked(data, event):
                    entered.set()
                    self.assertTrue(event.wait(2), "control change must cancel active provider")
                    raise SemanticError("cancelled", "Cancelled")

                provider.mutate = blocked

                def observe():
                    try:
                        controller.get_semantic_state()
                    except ControllerError as exc:
                        errors.append(exc.code)

                worker = threading.Thread(target=observe)
                worker.start()
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                getattr(controller, operation)("17") if operation == "select_window" else getattr(controller, operation)()
                self.assertLess(time.monotonic() - started, 1)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, ["cancelled"])
                controller.close()

    def test_expiry_is_checked_after_semantic_revalidation(self):
        state = self.controller.get_semantic_state()
        self.provider.mutate = lambda d, e: setattr(self.controller, "observation_max_age", -1)
        self.assert_code("expired_observation", lambda: self.controller.execute("click", state["observation_id"], x=20, y=30))
        self.assertFalse(self.backend.actions)

    def test_disarmed_read_uses_separate_cancellation_and_image_mode_unchanged(self):
        self.controller.disarm()
        state = self.controller.get_semantic_state()
        self.assertFalse(state["armed"])
        self.assertEqual(self.provider.calls, 1)
        image = self.controller.get_window_state()
        self.assertNotIn("semantic", image)
        self.assertEqual(self.provider.calls, 1)

    def test_disabled_provider_and_closed_controller_have_no_receipts(self):
        controller = Controller(self.backend)
        controller.select_window("17")
        self.assert_code("semantic_unavailable", controller.get_semantic_state)
        self.controller.close()
        self.assert_code("closed", self.controller.get_semantic_state)
        self.assertTrue(self.provider.closed)


class SemanticFormatTests(unittest.TestCase):
    def setUp(self):
        self.window = {"id": "17", "pid": 42, "title": "Fixture", "rect": {"x": 0, "y": 0, "width": 640, "height": 400}}

    def test_canonical_hash_ignores_snapshot_ids_timings_and_dict_key_order(self):
        first = snapshot(self.window)
        second = copy.deepcopy(first)
        second.update(snapshot_id="other", metrics={"inspect_ms": 999})
        second["nodes"][1] = dict(reversed(list(second["nodes"][1].items())))
        a, b = compact_semantic(first, self.window), compact_semantic(second, self.window)
        self.assertEqual(a, b)
        self.assertEqual(semantic_fingerprint(a), semantic_fingerprint(b))
        second["nodes"][1]["enabled"] = False
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(compact_semantic(second, self.window)))

    def test_protected_text_or_bad_bounds_are_rejected(self):
        for mutate in (lambda n: n.update(password=True), lambda n: n["bounds"].update(x=-1)):
            data = snapshot(self.window)
            mutate(data["nodes"][1])
            with self.assertRaises(SemanticError):
                compact_semantic(data, self.window)

    def test_model_projection_never_contains_image_or_authority(self):
        state = {"observation_id": "receipt", "window": self.window, "semantic": compact_semantic(snapshot(self.window), self.window),
                 "image_base64": "secret", "controller_instance_id": "private", "semantic_fingerprint": "private"}
        self.assertEqual(set(semantic_projection(state)), {"observation_id", "width", "height", "semantic"})

    def test_unknown_password_is_incomplete_and_invalid_table_is_a_fault(self):
        data = snapshot(self.window)
        data["nodes"][1].update(password=None)
        del data["nodes"][1]["name"]
        with self.assertRaises(SemanticError) as caught:
            compact_semantic(data, self.window)
        self.assertEqual(caught.exception.code, "semantic_incomplete")
        table = compact_semantic(snapshot(self.window), self.window)
        self.assertEqual(validate_semantic(table, self.window), table)
        table["rows"][1][5] = True
        with self.assertRaises(SemanticError) as caught:
            validate_semantic(table, self.window)
        self.assertEqual(caught.exception.code, "semantic_provider_failed")


class NativeProviderTests(unittest.TestCase):
    def test_lazy_lifecycle_business_error_retention_and_terminal_no_restart(self):
        class Client:
            def __init__(self, path, *, cancel_event):
                created.append(self)
                self.hello = {"methods": ["inspect_accessibility"]}
                self.error = None
                self.closed = False
            def call(self, method, **kwargs):
                if self.error:
                    raise NativeError(self.error, "Do not expose raw provider text")
                return snapshot(window)
            def close(self):
                self.closed = True

        window = {"id": "17", "pid": 42, "title": "Fixture", "rect": {"x": 0, "y": 0, "width": 640, "height": 400}}
        created = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native helper.exe"
            path.touch()
            provider = NativeSemanticProvider(path, client_factory=Client)
            self.assertEqual(provider.status(), {"configured": True, "state": "uninitialized"})
            self.assertFalse(created)
            event = threading.Event()
            provider.inspect(window, cancel_event=event)
            created[0].error = "accessibility_unavailable"
            with self.assertRaises(SemanticError) as caught:
                provider.inspect(window, cancel_event=event)
            self.assertEqual(caught.exception.code, "semantic_unavailable")
            self.assertEqual(provider.status()["state"], "ready")
            created[0].error = "pid_mismatch"
            with self.assertRaises(SemanticError) as caught:
                provider.inspect(window, cancel_event=event)
            self.assertEqual(caught.exception.code, "target_changed")
            self.assertEqual(provider.status()["state"], "ready")
            created[0].error = "timeout"
            with self.assertRaises(SemanticError):
                provider.inspect(window, cancel_event=event)
            self.assertEqual(provider.status()["state"], "unavailable")
            with self.assertRaises(SemanticError):
                provider.inspect(window, cancel_event=event)
            self.assertEqual(len(created), 1)
            self.assertNotIn("text", repr(provider.status()))
            provider.close()
            provider.close()
            self.assertTrue(created[0].closed)

    def test_close_interrupts_lazy_hello_without_restart(self):
        entered = threading.Event()
        errors = []
        created = []

        def factory(path, *, cancel_event):
            created.append(path)
            entered.set()
            if not cancel_event.wait(2):
                raise AssertionError("lazy hello must be cancelled")
            raise NativeError("cancelled", "Cancelled")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native helper.exe"
            path.touch()
            provider = NativeSemanticProvider(path, client_factory=factory)

            def read():
                try:
                    provider.inspect({}, cancel_event=threading.Event())
                except SemanticError as exc:
                    errors.append(exc.code)

            worker = threading.Thread(target=read)
            worker.start()
            self.assertTrue(entered.wait(2))
            provider.close()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["cancelled"])
            self.assertEqual(provider.status()["state"], "closed")
            with self.assertRaises(SemanticError):
                provider.inspect({}, cancel_event=threading.Event())
            self.assertEqual(len(created), 1)

    def test_failed_handshake_never_retries_or_masquerades_as_fallback(self):
        created = []

        def factory(path, *, cancel_event):
            created.append(path)
            raise NativeError("not_supported", "Bad hello response")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native helper.exe"
            path.touch()
            provider = NativeSemanticProvider(path, client_factory=factory)
            for _ in range(2):
                with self.assertRaises(SemanticError) as caught:
                    provider.inspect({}, cancel_event=threading.Event())
                self.assertEqual(caught.exception.code, "semantic_provider_failed")
            self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
