import threading
import time
from types import SimpleNamespace
import unittest

from opencomputer.backend import BackendError, FakeBackend, WindowsBackend, parse_key
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
        self.controller.execute("click", observation, x=5, y=5)
        self.error("stale_observation", lambda: self.controller.execute("click", observation, x=5, y=5))
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(self.events[-1]["x"], 105)

    def test_latest_observation_replaces_previous(self):
        first = self.observe_arm()
        second = self.controller.get_window_state()["observation_id"]
        self.error("stale_observation", lambda: self.controller.execute("activate", first))
        self.controller.execute("activate", second)

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

    def test_ordinary_action_does_not_implicitly_activate(self):
        self.backend.activate = lambda _: self.fail("Ordinary action must not activate")
        self.backend._send = lambda *args: None
        self.backend.perform("move", "1", x=1, y=1)

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


if __name__ == "__main__":
    unittest.main()
