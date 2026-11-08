import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from opencomputer import setup


class SetupTests(unittest.TestCase):
    def test_explicit_native_helper_validation_never_searches_or_executes(self):
        with tempfile.TemporaryDirectory(prefix="helper path ") as directory:
            helper = Path(directory) / "observer.exe"
            helper.touch()
            self.assertEqual(setup.validate_native_helper(helper), helper.resolve())
            for invalid in ("observer.exe", helper.parent, helper.with_suffix(".cmd"), helper.parent / "missing.exe"):
                with self.assertRaises(ValueError):
                    setup.validate_native_helper(invalid)

    @patch.object(setup, "dependency_checks", return_value=[{"name": "deps", "ok": True}])
    def test_offline_doctor_helper_stat_does_not_execute_or_connect(self, _checks):
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, "Popen") as launch:
            helper = Path(directory) / "observer.exe"
            helper.touch()
            result = setup.run_doctor(include_live=False, native_helper=helper,
                client_factory=lambda _: self.fail("Offline doctor must not connect"))
        self.assertTrue(result["ok"])
        self.assertIn("protocol not checked", result["checks"][-1]["detail"])
        self.assertNotIn(str(helper), json.dumps(result))
        launch.assert_not_called()

    def test_doctor_rejects_untested_pillow_versions(self):
        for version, expected in (("12.1.0", False), ("12.2.0", True), ("13.0.0", False)):
            with self.subTest(version=version), patch.object(setup.metadata, "version", return_value=version):
                pillow = next(check for check in setup.dependency_checks() if check["name"] == "pillow")
                self.assertEqual(pillow["ok"], expected)

    def test_config_is_credential_free_and_absolute(self):
        config = setup.build_mcp_config()
        server = config["mcpServers"]["open-computer"]
        self.assertTrue(Path(server["command"]).is_absolute())
        self.assertEqual(server["args"], ["-P", "-m", "opencomputer.mcp"])
        self.assertTrue(Path(server["env"]["PYTHONPATH"]).is_absolute())
        self.assertNotIn("token", json.dumps(config).lower())

    def test_config_rejects_missing_interpreter_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                setup.build_mcp_config(Path(temporary) / "missing.exe")
            with self.assertRaises(ValueError):
                setup.build_mcp_config(package_root=temporary)

    def test_config_imports_from_path_with_spaces_outside_cwd(self):
        with tempfile.TemporaryDirectory(prefix="Open Computer setup ") as temporary:
            root = Path(temporary)
            package_root = root / "package with spaces"
            shutil.copytree(Path(setup.__file__).parent, package_root / "opencomputer",
                            ignore=shutil.ignore_patterns("__pycache__"))
            outside = root / "unrelated working directory"
            outside.mkdir()
            config = setup.build_mcp_config(package_root=package_root)
            server = config["mcpServers"]["open-computer"]
            result = subprocess.run([server["command"], *server["args"], "--help"],
                                    env={**os.environ, **server["env"]}, cwd=outside,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--connection", result.stdout)

    def test_generic_and_unsloth_formats_match(self):
        outputs = []
        for format_name in ("generic", "unsloth"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(setup.main(["config", "--format", format_name]), 0)
            outputs.append(json.loads(output.getvalue()))
        self.assertEqual(outputs[0], outputs[1])

    @patch.object(setup, "dependency_checks", return_value=[{"name": "deps", "ok": True}])
    def test_doctor_reads_status_without_actions_or_credentials(self, _checks):
        calls = []
        class Client:
            def call(self, name):
                calls.append(name)
                return {"armed": False, "stopped": False, "backend": "demo"}
        with tempfile.TemporaryDirectory() as temporary:
            connection = Path(temporary) / "connection.json"
            connection.write_text('{"token":"DO_NOT_OUTPUT"}')
            result = setup.run_doctor(connection, client_factory=lambda _: Client())
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["status"])
        self.assertNotIn("DO_NOT_OUTPUT", json.dumps(result))

    @patch.object(setup, "dependency_checks", return_value=[{"name": "deps", "ok": True}])
    def test_doctor_armed_or_unreachable_fails_without_raw_errors(self, _checks):
        class ArmedClient:
            def call(self, name):
                return {"armed": True}
        with tempfile.TemporaryDirectory() as temporary:
            connection = Path(temporary) / "connection.json"
            connection.touch()
            result = setup.run_doctor(connection, client_factory=lambda _: ArmedClient())
            self.assertFalse(result["ok"])
            def failure(_):
                raise ValueError("secret-token-content")
            result = setup.run_doctor(connection, client_factory=failure)
            self.assertFalse(result["ok"])
            self.assertNotIn("secret-token-content", json.dumps(result))

    @patch.object(setup, "dependency_checks", return_value=[{"name": "deps", "ok": True}])
    def test_offline_doctor_never_contacts_gui(self, _checks):
        def forbidden(_):
            self.fail("Offline doctor must not contact GUI")
        result = setup.run_doctor(include_live=False, client_factory=forbidden)
        self.assertTrue(result["ok"])
        self.assertFalse(result["gui"]["checked"])


if __name__ == "__main__":
    unittest.main()
