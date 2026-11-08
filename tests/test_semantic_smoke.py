"""Fake-only smoke harness tests. No native API, GUI, socket or model calls."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import semantic_smoke as smoke
from scripts import win32_semantic_fixture as fixture
from opencomputer.backend import BackendError


def metadata():
    controls = [{**control, "hwnd": index + 1000} for index, control in
                enumerate(fixture.control_specs(True))]
    result = fixture.fixture_metadata(123, 456, controls)
    result["timeout_seconds"] = 180
    return result


def window():
    return {"id": "456", "pid": 123, "title": smoke.EXPECTED_TITLE,
            "rect": {"x": 80, "y": 100, "width": 720, "height": 440}}


def state():
    return {"observation_id": "fake-observation", "image_base64": "ZmFrZS1waXhlbHM=",
        "mime_type": "image/png", "window": window(), "observation_kind": "semantic",
        "semantic": {"format": "controls-v1",
            "columns": ["id", "parent", "role", "name", "value", "x", "y", "width", "height",
                        "enabled", "focusable", "password"],
            "rows": [["e0", None, "window", "Fixture", None, 0, 0, 720, 440, True, False, False],
                     ["e1", "e0", "button", "Test click", None, 32, 126, 150, 40, True, True, False],
                     ["e2", "e0", "text", "Clicks: 0", None, 32, 182, 250, 28, True, False, False],
                     ["e3", "e0", "edit", None, None, 32, 290, 648, 32, True, True, True]]}}


class SemanticSmokeTests(unittest.TestCase):
    def test_no_arguments_and_help_never_construct_native_components(self):
        with patch.object(smoke, "WindowsBackend") as backend, patch.object(smoke, "make_provider") as provider, \
                patch.object(smoke, "start_server") as server, redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main([]), 0)
            with self.assertRaises(SystemExit):
                smoke.main(["--help"])
        backend.assert_not_called()
        provider.assert_not_called()
        server.assert_not_called()

    def test_explicit_run_requires_fixture_and_executable_before_native_work(self):
        with patch.object(smoke, "WindowsBackend") as native, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                smoke.main(["--run"])
        native.assert_not_called()
        with self.assertRaises(smoke.BenchmarkError):
            smoke.run_smoke({}, Path("unused.exe"), Path("unused"))

    def test_spy_never_delegates_input_and_rejects_other_identity(self):
        reader = Mock()
        reader.get_window.return_value = window()
        backend = smoke.SpyInputBackend(metadata(), reader)
        backend.perform("click", "456", x=107, y=146)
        self.assertEqual(len(backend.actions), 1)
        reader.perform.assert_not_called()
        with self.assertRaises(BackendError):
            backend.get_window("999")
        self.assertEqual(reader.get_window.call_count, 1)
        reader.get_window.return_value = {**window(), "pid": 999}
        with self.assertRaises(BackendError):
            backend.capture("456")
        reader.capture.assert_not_called()

    def test_text_projection_redacts_password_and_pins_target_geometry(self):
        projection, point = smoke.inspect_projection(state())
        self.assertEqual(point, (107, 146))
        self.assertNotIn("image_base64", projection)
        for changed in ("password", "bounds"):
            value = state()
            if changed == "password":
                value["semantic"]["rows"][-1][3] = fixture.PASSWORD_SENTINEL
            else:
                value["semantic"]["rows"][1][5] += 1
            with self.assertRaises(smoke.BenchmarkError) as rejected:
                smoke.inspect_projection(value)
            self.assertNotIn(fixture.PASSWORD_SENTINEL, str(rejected.exception))

    def test_fake_end_to_end_routing_has_ephemeral_server_and_always_closes(self):
        provider, server = Mock(), Mock(url="http://127.0.0.1:1", token="fixture-token")
        reader = Mock()
        reader.get_window.side_effect = lambda _: deepcopy(window())
        holder = {}

        class Controller:
            def __init__(self, backend, semantic_provider):
                self.backend, self.provider, self.armed, self.used = backend, semantic_provider, False, False
                self.close = Mock(side_effect=semantic_provider.close)
                holder["controller"] = self

            def select_window(self, identifier):
                self.backend.get_window(identifier)

            def local_arm(self):
                self.armed = True

            def stop(self):
                self.armed = False

        class Client:
            def call(self, name, arguments=None):
                controller = holder["controller"]
                if name == "status":
                    return {"armed": controller.armed}
                if name == "get_semantic_state":
                    return state()
                if name == "click":
                    if controller.used:
                        error = RuntimeError("fake replay")
                        error.code = "stale_observation"
                        raise error
                    controller.backend.perform("click", "456", x=arguments["x"], y=arguments["y"])
                    controller.used = True

        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "observer.exe"
            helper.touch()
            with patch.object(smoke, "WindowsBackend", return_value=reader), \
                    patch.object(smoke, "make_provider", return_value=provider), \
                    patch.object(smoke, "Controller", Controller), \
                    patch.object(smoke, "start_server", return_value=server) as start, \
                    patch.object(smoke, "LocalClient", return_value=Client()):
                result = smoke.run_smoke(metadata(), helper, Path(temporary) / "output", authorized=True)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["native_input_executed"])
        self.assertEqual(result["model_requests"], 0)
        self.assertEqual(start.call_args.kwargs, {"port": 0, "connection_path": None})
        self.assertFalse(holder["controller"].armed)
        reader.perform.assert_not_called()
        server.close.assert_called_once()
        provider.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
