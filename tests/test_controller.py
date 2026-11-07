import os
import base64
import random
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opencomputer.backend import BackendCancelled, BackendError, FakeBackend, WindowsBackend, parse_key
from opencomputer.controller import Controller, ControllerError


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.events = []
        self.controller = Controller(self.backend, self.events.append)
        self.controller.select_window("demo")

    def error(self, code, callback):
        with self.assertRaises(ControllerError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def observe_arm(self):
        self.controller.arm()
        return self.controller.get_window_state()["observation_id"]

    def test_disarmed_actions_and_single_use_receipts(self):
        observation = self.controller.get_window_state()["observation_id"]
        self.error("not_armed", lambda: self.controller.execute("click", observation, x=5, y=5))
        self.controller.arm()
        observation = self.controller.get_window_state()["observation_id"]
        self.controller.execute("click", observation, x=5, y=5)
        self.error("stale_observation", lambda: self.controller.execute("click", observation, x=5, y=5))
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(self.events[-1]["x"], 105)

    def test_latest_observation_replaces_previous(self):
        first = self.observe_arm()
        second = self.controller.get_window_state()["observation_id"]
        self.error("stale_observation", lambda: self.controller.execute("activate", first))
        self.controller.execute("activate", second)

    def test_local_authority_generation_changes_on_every_control_transition(self):
        generation = self.controller.status()["control_generation"]
        for operation in (self.controller.arm, self.controller.disarm,
                          self.controller.reset_stop, self.controller.arm,
                          self.controller.stop, self.controller.reset_stop):
            state = operation()
            self.assertGreater(state["control_generation"], generation)
            generation = state["control_generation"]
        self.controller.select_window("demo")
        self.assertGreater(self.controller.status()["control_generation"], generation)
        state = self.controller.get_window_state()
        self.assertEqual(state["control_generation"], self.controller.status()["control_generation"])

    def test_arm_invalidates_observation_from_previous_authority(self):
        observation = self.controller.get_window_state()["observation_id"]
        self.controller.arm()
        self.error("stale_observation", lambda: self.controller.execute("activate", observation))

    def test_instance_id_matches_capture_and_survives_control_transitions(self):
        instance_id = self.controller.status()["controller_instance_id"]
        self.assertRegex(instance_id, r"^[0-9a-f]{32}$")
        self.assertEqual(self.controller.get_window_state()["controller_instance_id"], instance_id)
        self.controller.arm()
        self.controller.stop()
        self.controller.reset_stop()
        self.assertEqual(self.controller.status()["controller_instance_id"], instance_id)

    def test_new_controller_instance_cannot_reuse_previous_authority_epoch(self):
        previous = self.controller.status()
        replacement = Controller(FakeBackend())
        replacement.select_window("demo")
        current = replacement.status()
        self.assertEqual(current["selected_window"], previous["selected_window"])
        self.assertEqual(current["control_generation"], previous["control_generation"])
        self.assertNotEqual(current["controller_instance_id"], previous["controller_instance_id"])

    def test_expired_observation(self):
        self.controller.observation_max_age = -1
        observation = self.observe_arm()
        self.error("expired_observation", lambda: self.controller.execute("activate", observation))
        self.assertFalse(self.backend.actions)

    def test_move_and_process_identity_changes_rejected(self):
        observation = self.observe_arm()
        self.backend.window["rect"]["x"] += 1
        self.error("target_changed", lambda: self.controller.execute("activate", observation))
        observation = self.controller.get_window_state()["observation_id"]
        self.backend.window["pid"] = 42
        self.error("target_changed", lambda: self.controller.execute("activate", observation))
        self.assertFalse(self.controller.status()["armed"])

    def test_invalid_points_and_params_consume_receipt(self):
        for params in ({"x": -1, "y": 2}, {"x": True, "y": 2}, {"x": 640, "y": 2}):
            observation = self.observe_arm()
            self.error("invalid_coordinates", lambda: self.controller.execute("click", observation, **params))
            self.assertIsNone(self.controller.status()["observation_id"])
        observation = self.observe_arm()
        self.error("invalid_action", lambda: self.controller.execute("move", observation, x=1, y=1, surprise=1))
        self.assertFalse(self.backend.actions)

    def test_invalid_key_and_drag_duration(self):
        observation = self.observe_arm()
        self.error("invalid_arguments", lambda: self.controller.execute("press_key", observation, key="invalid"))
        observation = self.observe_arm()
        self.error("invalid_arguments", lambda: self.controller.execute("drag", observation, x=1, y=1, to_x=3, to_y=3, duration=float("nan")))
        self.assertEqual(parse_key("Ctrl+Shift+A"), [17, 16, 65])

    def test_invalid_unicode_rejected_before_any_input(self):
        observation = self.observe_arm()
        self.error("invalid_arguments", lambda: self.controller.execute("type_text", observation, text="prefix\ud800"))
        self.assertFalse(self.backend.actions)

    def test_selection_disarms_and_cannot_reset_emergency_stop(self):
        self.observe_arm()
        self.controller.stop()
        self.controller.select_window("demo")
        self.assertTrue(self.controller.status()["stopped"])
        self.error("stopped", self.controller.arm)
        self.controller.reset_stop()
        self.assertFalse(self.controller.status()["armed"])
        self.controller.arm()
        self.controller.select_window("demo")
        self.assertFalse(self.controller.status()["armed"])

    def test_disarm_can_be_locally_rearmed(self):
        self.observe_arm()
        self.controller.disarm()
        self.assertFalse(self.controller.status()["stopped"])
        self.controller.arm()
        self.assertTrue(self.controller.status()["armed"])

    def test_disarmed_action_reports_not_armed_without_emergency_reset(self):
        observation = self.observe_arm()
        self.controller.disarm()
        self.error("not_armed", lambda: self.controller.execute("activate", observation))

    def test_disarm_during_action_preserves_ordinary_disarm(self):
        entered = threading.Event()
        errors = []

        def blocking(action, window_id, **params):
            entered.set()
            self.backend.cancel_event.wait(2)
            FakeBackend.perform(self.backend, action, window_id, **params)

        self.backend.perform = blocking
        observation = self.observe_arm()

        def run():
            try:
                self.controller.execute("activate", observation)
            except ControllerError as exc:
                errors.append(exc.code)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.controller.disarm()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertFalse(self.controller.status()["stopped"])
        self.controller.arm()
        self.assertTrue(self.controller.status()["armed"])

    def test_selection_and_stop_cancel_without_rearming_old_action(self):
        for local_change in (self.controller.select_window, self.controller.stop):
            with self.subTest(change=local_change.__name__):
                self.controller.reset_stop()
                entered = threading.Event()
                errors = []

                def blocking(action, window_id, **params):
                    entered.set()
                    self.backend.cancel_event.wait(2)
                    FakeBackend.perform(self.backend, action, window_id, **params)

                def run():
                    try:
                        self.controller.execute("activate", observation)
                    except ControllerError as exc:
                        errors.append(exc.code)

                self.backend.perform = blocking
                observation = self.observe_arm()
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(entered.wait(1))
                if local_change.__name__ == "select_window":
                    local_change("demo")
                else:
                    local_change()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, ["cancelled"])
                self.assertFalse(self.controller.status()["armed"])
                self.assertEqual(self.controller.status()["stopped"], local_change.__name__ == "stop")
                self.assertFalse(self.backend.actions)

    def test_stop_interrupts_running_action_without_waiting_for_lock(self):
        entered = threading.Event()
        errors = []

        def blocking(action, window_id, **params):
            entered.set()
            self.backend.cancel_event.wait(3)
            raise BackendError("Stopped")

        self.backend.perform = blocking
        observation = self.observe_arm()

        def run():
            try:
                self.controller.execute("activate", observation)
            except ControllerError as exc:
                errors.append(exc.code)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        self.controller.stop()
        self.assertLess(time.monotonic() - start, 0.5)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["input_rejected"])
        self.assertFalse(self.controller.status()["armed"])

    def test_unexpected_backend_failure_stops_control(self):
        def fail(*args, **kwargs):
            raise OSError("native failure")
        self.backend.perform = fail
        observation = self.observe_arm()
        self.error("input_rejected", lambda: self.controller.execute("activate", observation))
        self.assertTrue(self.controller.status()["stopped"])

    def test_overlay_failure_fails_closed(self):
        def fail(event):
            raise RuntimeError("overlay offline")
        self.controller.event_sink = fail
        self.error("overlay_error", self.controller.arm)
        self.assertTrue(self.controller.status()["stopped"])
        self.assertFalse(self.controller.status()["armed"])


class BackendSafetyTests(unittest.TestCase):
    """Exercise native sequencing with every actual Win32 input call replaced."""

    def setUp(self):
        self.backend = WindowsBackend.__new__(WindowsBackend)
        self.backend.cancel_event = threading.Event()
        self.backend._title_cache = {}
        self.backend.user32 = SimpleNamespace(SendInput=lambda *args: 0)
        self.backend._create_input_types()
        self.window = {"id": "1", "pid": 42, "rect": FakeBackend().window["rect"]}
        self.backend.get_window = lambda _: self.window
        self.backend._check = lambda *args: None
        self.backend._move = lambda *args: (101, 101)
        self.calls = []

    def test_partial_mouse_press_failure_still_releases(self):
        def send(item):
            self.calls.append(item.mi.dwFlags)
            if len(self.calls) == 1:
                raise BackendError("partial failure")
        self.backend._send = send
        with self.assertRaises(BackendError):
            self.backend.perform("click", "1", x=1, y=1)
        self.assertEqual(self.calls, [2, 4])

    def test_partial_keyboard_failure_releases_every_attempted_key(self):
        def send(item):
            self.calls.append((item.ki.wVk, item.ki.dwFlags))
            if item.ki.wVk == 65 and item.ki.dwFlags == 0:
                raise BackendError("partial failure")
        self.backend._send = send
        with self.assertRaises(BackendError):
            self.backend.perform("press_key", "1", key="Ctrl+A")
        self.assertEqual(self.calls, [(17, 0), (65, 0), (65, 2), (17, 2)])

    def test_cancelled_drag_releases_mouse(self):
        def send(item):
            self.calls.append(item.mi.dwFlags)
            if item.mi.dwFlags == 2:
                self.backend.cancel_event.set()
        self.backend._send = send
        with self.assertRaises(BackendError):
            self.backend.perform("drag", "1", x=1, y=1, to_x=10, to_y=10, duration=0.1)
        self.assertEqual(self.calls, [2, 4])

    def test_navigation_keys_include_extended_flag_on_down_and_up(self):
        self.backend._send = lambda item: self.calls.append((item.ki.wVk, item.ki.dwFlags))
        self.backend.perform("press_key", "1", key="Ctrl+Home")
        self.assertEqual(self.calls, [(17, 0), (36, 1), (36, 3), (17, 2)])

    def test_unicode_surrogates_remain_together_when_cancelled(self):
        def check(*args):
            if self.backend.cancel_event.is_set():
                raise BackendError("Stopped")

        def send(*items):
            self.calls.append([(item.ki.wScan, item.ki.dwFlags) for item in items])
            self.backend.cancel_event.set()

        self.backend._check = check
        self.backend._send = send
        with self.assertRaises(BackendError):
            self.backend.perform("type_text", "1", text="\U0001f680x")
        self.assertEqual(self.calls, [[(0xD83D, 4), (0xD83D, 6), (0xDE80, 4), (0xDE80, 6)]])

    def test_partial_unicode_injection_releases_all_code_units(self):
        def send(*items):
            self.calls.append([(item.ki.wScan, item.ki.dwFlags) for item in items])
            if len(self.calls) == 1:
                raise BackendError("Partial input")

        self.backend._send = send
        with self.assertRaises(BackendError):
            self.backend.perform("type_text", "1", text="\U0001f680")
        self.assertEqual(self.calls[-1], [(0xD83D, 6), (0xDE80, 6)])

    def test_ordinary_action_does_not_implicitly_activate(self):
        self.backend.activate = lambda _: self.fail("Ordinary action must not activate")
        self.backend._send = lambda *args: None
        self.backend.perform("move", "1", x=1, y=1)

    def test_activation_rejects_geometry_change_before_focusing(self):
        self.backend.activate = lambda _: self.calls.append("activated")
        previous = {**self.window["rect"], "x": 99}
        with self.assertRaisesRegex(BackendError, "geometry"):
            self.backend.perform("activate", "1", _expected_rect=previous, _expected_pid=42)
        self.assertEqual(self.calls, [])

    def test_drag_rechecks_cancellation_after_initial_pointer_move(self):
        def check(*args):
            if self.backend.cancel_event.is_set():
                raise BackendCancelled("Cancelled")
        def move(*args):
            self.backend.cancel_event.set()
            return (101, 101)
        self.backend._check = check
        self.backend._move = move
        self.backend._send = lambda *items: self.calls.extend(items)
        with self.assertRaises(BackendCancelled):
            self.backend.perform("drag", "1", x=1, y=1, to_x=5, to_y=5)
        self.assertEqual(self.calls, [])

    def test_native_focus_pid_and_occlusion_guards(self):
        self.backend._handle = lambda _: 1
        self.backend._expected_pid = 42
        self.backend.user32.GetForegroundWindow = lambda: 2
        self.backend.user32.GetAsyncKeyState = lambda _: 0
        self.backend.user32.WindowFromPoint = lambda _: 2
        self.backend.user32.GetAncestor = lambda *args: 2
        rect = self.window["rect"]
        check = lambda: WindowsBackend._check(self.backend, "1", rect, (101, 101))
        with self.assertRaisesRegex(BackendError, "foreground"):
            check()
        self.backend.user32.GetForegroundWindow = lambda: 1
        with self.assertRaisesRegex(BackendError, "covered"):
            check()
        self.backend.user32.GetAncestor = lambda *args: 1
        self.backend._expected_pid = 43
        with self.assertRaisesRegex(BackendError, "identity"):
            check()

    def test_own_foreign_thread_caption_never_calls_synchronous_text_apis(self):
        def forbidden(*args):
            self.fail("Same-process cross-thread caption must not send synchronous messages")
        self.backend.user32.GetWindowTextLengthW = forbidden
        self.backend.user32.GetWindowTextW = forbidden
        other_thread = threading.get_native_id() + 1
        self.assertEqual(self.backend._window_title(123, os.getpid(), other_thread), "Window 123")
        self.backend._title_cache[(123, os.getpid(), other_thread)] = "Cached fixture"
        self.assertEqual(self.backend._window_title(123, os.getpid(), other_thread), "Cached fixture")


class CaptureEncodingTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.backend = WindowsBackend.__new__(WindowsBackend)
        self.backend._capture_cache = None
        self.backend._capture_cache_lock = threading.Lock()
        self.backend._capture_cache_limit = 32 * 1024 * 1024
        self.window = {"id": "1", "pid": 42, "rect": FakeBackend().window["rect"]}
        self.window["rect"].update(width=16, height=16, right=116, bottom=116)
        self.backend.get_window = lambda window_id: {**self.window, "id": str(window_id)}
        self.image = Image.new("RGB", (16, 16), "#102030")

    def test_unchanged_frame_reuses_original_png_but_always_grabs_fresh_pixels(self):
        expected = self.backend._encode_png(self.image)
        with patch("PIL.ImageGrab.grab", return_value=self.image) as grab, \
                patch.object(self.backend, "_encode_png", wraps=self.backend._encode_png) as encode:
            first = self.backend.capture("1")
            second = self.backend.capture("1")
        self.assertEqual(first["image_base64"], expected)
        self.assertEqual(second["image_base64"], expected)
        self.assertEqual(grab.call_count, 2)
        self.assertEqual(encode.call_count, 1)

    def test_changed_pixel_geometry_target_or_pid_invalidates_encoding(self):
        with patch("PIL.ImageGrab.grab", return_value=self.image), \
                patch.object(self.backend, "_encode_png", wraps=self.backend._encode_png) as encode:
            original = self.backend.capture("1")
            self.image.putpixel((1, 1), (255, 0, 0))
            changed = self.backend.capture("1")
            self.assertNotEqual(original["image_base64"], changed["image_base64"])
            self.window["rect"]["x"] += 1
            self.backend.capture("1")
            self.backend.capture("2")
            self.window["pid"] += 1
            self.backend.capture("2")
        self.assertEqual(encode.call_count, 5)

    def test_cache_budget_includes_raw_pixels_and_base64(self):
        from PIL import Image
        image = Image.frombytes("RGB", (16, 16), random.Random(42).randbytes(16 * 16 * 3))
        self.backend._capture_cache_limit = 1500
        with patch("PIL.ImageGrab.grab", return_value=image):
            state = self.backend.capture("1")
        self.assertGreater(len(image.tobytes()) + len(state["image_base64"]), 1500)
        self.assertIsNone(self.backend._capture_cache)
        self.assertTrue(base64.b64decode(state["image_base64"]).startswith(b"\x89PNG"))

    def test_oversized_image_skips_raw_copy_and_is_not_cached(self):
        self.backend._capture_cache_limit = 100
        with patch("PIL.ImageGrab.grab", return_value=self.image), \
                patch.object(self.image, "tobytes", side_effect=AssertionError("Oversized raw copy")):
            self.backend.capture("1")
        self.assertIsNone(self.backend._capture_cache)

    def test_capture_error_or_geometry_change_clears_cache(self):
        with patch("PIL.ImageGrab.grab", return_value=self.image):
            self.backend.capture("1")
        self.assertIsNotNone(self.backend._capture_cache)
        with patch("PIL.ImageGrab.grab", side_effect=OSError("Capture failed")):
            with self.assertRaises(OSError):
                self.backend.capture("1")
        self.assertIsNone(self.backend._capture_cache)
        with patch("PIL.ImageGrab.grab", return_value=self.image):
            self.backend.capture("1")
            changed = {**self.window, "rect": {**self.window["rect"], "x": 99}}
            with patch.object(self.backend, "get_window", side_effect=[self.window, changed]):
                with self.assertRaises(BackendError):
                    self.backend.capture("1")
        self.assertIsNone(self.backend._capture_cache)

    def test_concurrent_identical_encodings_share_one_cache_entry(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch("PIL.ImageGrab.grab", return_value=self.image), \
                patch.object(self.backend, "_encode_png", wraps=self.backend._encode_png) as encode:
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(self.backend.capture, ["1", "1"]))
        self.assertEqual(results[0]["image_base64"], results[1]["image_base64"])
        self.assertEqual(encode.call_count, 1)


@unittest.skipUnless(sys.platform == "win32", "Windows caption/controller lock regression")
class OwnWindowCaptionTests(unittest.TestCase):
    def test_worker_status_releases_lock_without_tk_message_pumping(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        root.withdraw()  # No visible window, focus change, screenshot, or input.
        root.title("Open Computer read-only caption regression")
        root.geometry("200x100")
        root.update_idletasks()
        backend = WindowsBackend()
        hwnd = backend.user32.GetAncestor(root.winfo_id(), 2)
        # Only visibility is bypassed for this intentionally hidden fixture.
        # The real PID/client geometry/title metadata paths remain under test.
        backend._handle = lambda _window_id: hwnd
        controller = Controller(backend)
        controller.select_window(str(hwnd))
        backend._title_cache.clear()  # Exercise fallback, not just warm cache.
        entered = threading.Event()
        original_get_window = backend.get_window
        main_thread = threading.get_native_id()
        errors = []

        def get_window(window_id):
            if threading.get_native_id() != main_thread:
                entered.set()
            return original_get_window(window_id)
        backend.get_window = get_window

        def poll():
            try:
                controller.status()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            # Old code synchronously waited for WM_GETTEXT while owning this
            # lock; the UI's next status poll could then never return.
            acquired = controller._lock.acquire(timeout=0.5)
            if acquired:
                controller._lock.release()
            self.assertTrue(acquired, "Worker held the controller lock waiting for Tk")
            thread.join(0.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(controller.status()["selected_window"]["pid"], os.getpid())
        finally:
            deadline = time.monotonic() + 1
            while thread.is_alive() and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            root.destroy()
            thread.join(0.5)


if __name__ == "__main__":
    unittest.main()
