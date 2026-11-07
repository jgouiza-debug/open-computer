"""Pure presentation tests; no desktop input or GUI windows are created."""

import json
from pathlib import Path
import tempfile
import unittest

from opencomputer.app import event_description, panel_mcp_config
from opencomputer.overlay import rectangle_bounds


class OverlayPresentationTests(unittest.TestCase):
    def test_panel_config_uses_stdio_python_sibling_without_credentials(self):
        with tempfile.TemporaryDirectory(prefix="opencomputer panel ") as directory:
            base = Path(directory)
            python = base / "Python Runtime" / "python.exe"
            python.parent.mkdir()
            python.touch()
            pythonw = python.with_name("pythonw.exe")
            pythonw.touch()
            package = base / "Package Root" / "opencomputer"
            package.mkdir(parents=True)
            (package / "mcp.py").touch()
            config = json.loads(panel_mcp_config(pythonw, package.parent))
            server = config["mcpServers"]["open-computer"]
            self.assertEqual(server["command"], str(python.resolve()))
            self.assertEqual(server["args"], ["-P", "-m", "opencomputer.mcp"])
            self.assertEqual(set(server), {"command", "args", "env"})
            self.assertEqual(server["env"], {"PYTHONPATH": str(package.parent.resolve())})

    def test_panel_config_requires_python_exe_beside_pythonw(self):
        with tempfile.TemporaryDirectory() as directory:
            pythonw = Path(directory) / "pythonw.exe"
            pythonw.touch()
            with self.assertRaises(ValueError):
                panel_mcp_config(pythonw)

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
