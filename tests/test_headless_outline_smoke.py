"""Pure outline-harness helpers; no windows, model, or desktop input."""
import base64
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from scripts.headless_outline_smoke import (
    HealthProxy, REQUIRED_EXSTYLE, SpyBackend, WS_DISABLED, expected_bars, main, passive_style, pixels,
    rect_tuple, wait_until,
)


class OutlineProofTests(unittest.TestCase):
    def test_independent_bar_geometry_matches_each_client_edge(self):
        self.assertEqual(expected_bars((100, 200, 820, 640), 3, "outside"), [
            (97, 197, 823, 200), (97, 640, 823, 643), (97, 200, 100, 640), (820, 200, 823, 640)])
        self.assertEqual(expected_bars((0, 0, 720, 440), 3, "inset"), [
            (0, 0, 720, 3), (0, 437, 720, 440), (0, 3, 3, 437), (717, 3, 720, 437)])
        with self.assertRaises(RuntimeError):
            expected_bars((0, 0, 720, 440), 3, "unknown")

    def test_native_diagnostics_xywh_tuple_and_dictionary_aliases_agree(self):
        expected = (-50, 20, 670, 460)
        self.assertEqual(rect_tuple((-50, 20, 720, 440)), expected)
        self.assertEqual(rect_tuple([-50, 20, 720, 440]), expected)
        self.assertEqual(rect_tuple({"x": -50, "y": 20, "width": 720, "height": 440}), expected)
        self.assertEqual(rect_tuple(dict(zip(("left", "top", "right", "bottom"), expected))), expected)

    def test_passive_proof_requires_every_style_disabled_and_hit_transparency(self):
        self.assertTrue(passive_style(REQUIRED_EXSTYLE, WS_DISABLED, -1))
        for flag in (0x80000, 0x20, 0x80, 0x08000000):
            self.assertFalse(passive_style(REQUIRED_EXSTYLE & ~flag, WS_DISABLED, -1))
        self.assertFalse(passive_style(REQUIRED_EXSTYLE, 0, -1))
        self.assertFalse(passive_style(REQUIRED_EXSTYLE, WS_DISABLED, 1))

    def test_pixel_comparison_decodes_real_rgb_without_masking(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (3, 2), (11, 22, 33)).save(stream, format="PNG")
        frame = {"image_base64": base64.b64encode(stream.getvalue()).decode()}
        size, data = pixels(frame)
        self.assertEqual(size, (3, 2))
        self.assertEqual(data, bytes((11, 22, 33)) * 6)

    def test_spy_prohibits_all_input_and_nonowned_window_reads(self):
        backend = Mock()
        spy = SpyBackend(backend, 123)
        with self.assertRaises(RuntimeError):
            spy.get_window("456")
        backend.get_window.assert_not_called()
        with self.assertRaises(AssertionError):
            spy.perform("click", "123", x=1, y=2)
        backend.perform.assert_not_called()
        self.assertEqual(spy.input_attempts, ["click"])

    def test_health_fault_is_explicit_injection_and_delegates_normal_checks(self):
        outline = Mock()
        proxy = HealthProxy(outline)
        proxy.check_health()
        outline.check_health.assert_called_once()
        proxy.inject_failure = True
        with self.assertRaisesRegex(RuntimeError, "Injected"):
            proxy.check_health()

    def test_help_and_missing_optin_never_start_the_proof(self):
        with patch("scripts.headless_outline_smoke.run_proof") as proof, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as help_exit:
                main(["--help"])
            self.assertEqual(help_exit.exception.code, 0)
            with self.assertRaises(SystemExit) as optin_exit:
                main([])
            self.assertEqual(optin_exit.exception.code, 2)
            proof.assert_not_called()

    def test_polling_has_a_real_deadline_without_input_retry(self):
        predicate = Mock(return_value=False)
        with patch("scripts.headless_outline_smoke.time.monotonic", side_effect=[0, 0, .2]), \
                patch("scripts.headless_outline_smoke.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                wait_until(predicate, .1)
        predicate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
