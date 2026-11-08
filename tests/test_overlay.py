"""Pure presentation tests; no desktop input or GUI windows are created."""

import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
import io
from unittest.mock import Mock, patch

from opencomputer import app
from opencomputer.app import event_description, panel_mcp_config, semantic_status_text
from opencomputer.overlay import rectangle_bounds


class OverlayPresentationTests(unittest.TestCase):
    def test_native_helper_cli_rejects_demo_relative_and_missing_before_gui(self):
        with patch.object(app.tk, "Tk") as gui, redirect_stderr(io.StringIO()):
            for options in (["--native-helper", "relative.exe"],
                            ["--native-helper", "missing.exe", "--demo"],
                            ["--native-helper", "missing.exe", "--smoke-test"]):
                with self.assertRaises(SystemExit):
                    app.main(options)
        gui.assert_not_called()

    def test_semantic_status_is_allowlisted_and_does_not_claim_lazy_helper_ready(self):
        self.assertEqual(semantic_status_text({}), "Semantic helper: off")
        status = {"semantic_provider": {"configured": True, "state": "uninitialized", "path": "private"}}
        self.assertIn("configured", semantic_status_text(status))
        self.assertNotIn("ready", semantic_status_text(status))
        status["semantic_provider"]["state"] = "provider-secret-error"
        self.assertEqual(semantic_status_text(status), "Semantic helper: unavailable")

    def test_close_reaps_provider_even_if_server_or_hotkey_close_fails(self):
        for failing in ("hotkey", "server"):
            with self.subTest(failing=failing):
                panel = app.OpenComputerApp.__new__(app.OpenComputerApp)
                panel.closed = False
                panel.controller, panel.hotkey, panel.server = Mock(), Mock(), Mock()
                panel.overlay, panel.root = Mock(), Mock()
                getattr(panel, failing).close.side_effect = RuntimeError("fixture failure")
                with self.assertRaises(RuntimeError):
                    panel.close()
                panel.controller.stop.assert_called_once()
                panel.server.close.assert_called_once()
                panel.controller.close.assert_called_once()
                panel.overlay.destroy.assert_called_once()
                panel.root.destroy.assert_called_once()
                panel.close()  # Idempotent after partial teardown.

    def test_constructor_late_failure_reaps_partial_resources(self):
        root, controller, server, hotkey, overlay, provider = [Mock() for _ in range(6)]
        controller.close.side_effect = provider.close
        root.protocol.side_effect = RuntimeError("fake late Tk failure")

        def build_ui(panel):
            panel.connection, panel.endpoint, panel.connection_file = Mock(), Mock(), Mock()

        with patch.object(app.OpenComputerApp, "_build_ui", build_ui), \
                patch.object(app.OpenComputerApp, "_log"), \
                patch.object(app.OpenComputerApp, "refresh_windows"), \
                patch.object(app, "TargetOverlay", return_value=overlay), \
                patch.object(app, "SafetyHotkey", return_value=hotkey), \
                patch("opencomputer.server.start_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "fake late Tk failure"):
                app.OpenComputerApp(root, controller=controller)
        controller.stop.assert_called_once()
        controller.close.assert_called_once()
        provider.close.assert_called_once()
        server.close.assert_called_once()
        hotkey.close.assert_called_once()
        overlay.destroy.assert_called_once()
        root.destroy.assert_called_once()

    def test_constructor_early_ui_failure_handles_uncreated_hotkey(self):
        root, controller = Mock(), Mock()
        with patch.object(app.OpenComputerApp, "_build_ui", side_effect=RuntimeError("fake UI failure")), \
                patch.object(app, "SafetyHotkey") as hotkey:
            with self.assertRaisesRegex(RuntimeError, "fake UI failure"):
                app.OpenComputerApp(root, controller=controller)
        hotkey.assert_not_called()
        controller.stop.assert_called_once()
        controller.close.assert_called_once()
        root.destroy.assert_called_once()

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
