"""Pure presentation tests; no desktop input or GUI windows are created."""

import unittest

from opencomputer.app import event_description
from opencomputer.overlay import rectangle_bounds


class OverlayPresentationTests(unittest.TestCase):
    def test_relative_dimensions_preserve_negative_monitor_coordinates(self):
        self.assertEqual(rectangle_bounds({"x": -1920, "y": -100, "width": 800, "height": 600}),
                         (-1920, -100, -1120, 500))

    def test_native_rectangle(self):
        self.assertEqual(rectangle_bounds({"left": 20, "top": 30, "right": 220, "bottom": 330}),
                         (20, 30, 220, 330))

    def test_empty_target_rejected(self):
        with self.assertRaises(ValueError):
            rectangle_bounds({"x": 0, "y": 0, "width": 0, "height": 40})

    def test_activity_does_not_echo_typed_content(self):
        summary = event_description({"type": "action", "action": "type_text", "text": "sensitive words"})
        self.assertEqual(summary, "AI action: type_text")
        self.assertNotIn("sensitive", summary)

    def test_action_renders_absolute_coordinates(self):
        self.assertEqual(event_description({"type": "action", "action": "click", "x": -12, "y": 30}),
                         "AI action: click at (-12, 30)")


if __name__ == "__main__":
    unittest.main()
