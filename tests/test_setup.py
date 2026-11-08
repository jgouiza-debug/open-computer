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

    def test_config_normalizes_pythonw_to_existing_console_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            windowed = Path(temporary) / "pythonw.exe"
            console = windowed.with_name("python.exe")
            windowed.touch()
            with self.assertRaisesRegex(ValueError, "Console python.exe"):
                setup.build_mcp_config(windowed)
            console.touch()
            config = setup.build_mcp_config(windowed)
            self.assertEqual(config["mcpServers"]["open-computer"]["command"], str(console.resolve()))

    def test_frozen_gui_and_cli_use_only_the_sibling_console_without_source_files(self):
        with tempfile.TemporaryDirectory(prefix="portable café ") as temporary:
            root = Path(temporary)
            bundle = root / "Bundle With Spaces"
            bundle.mkdir()
            gui, console = bundle / "OpenComputer.exe", bundle / "OpenComputerCLI.exe"
            gui.touch()
            console.touch()
            decoy = root / "not-the-bundle"
            decoy.mkdir()
            (decoy / "OpenComputerCLI.exe").touch()
            for executable in (gui, console):
                with patch.object(setup.sys, "frozen", True, create=True), \
                        patch.object(setup.sys, "executable", str(executable)), \
                        patch.object(setup.sys, "_MEIPASS", str(decoy), create=True), \
                        patch.object(setup, "__file__", str(decoy / "missing" / "setup.py")):
                    config = setup.build_mcp_config(package_root=decoy / "missing-source")
                self.assertEqual(config, {"mcpServers": {"open-computer": {
                    "command": str(console.resolve()), "args": ["mcp"], "env": {}}}})
                self.assertNotIn("PYTHONPATH", json.dumps(config))

    def test_frozen_missing_sibling_fails_without_source_or_extraction_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, extraction = root / "bundle", root / "extraction"
            bundle.mkdir()
            extraction.mkdir()
            (extraction / "OpenComputerCLI.exe").touch()
            with patch.object(setup.sys, "frozen", True, create=True), \
                    patch.object(setup.sys, "executable", str(bundle / "OpenComputer.exe")), \
                    patch.object(setup.sys, "_MEIPASS", str(extraction), create=True):
                with self.assertRaisesRegex(ValueError, "sibling OpenComputerCLI.exe"):
                    setup.build_mcp_config()

    def test_explicit_python_override_keeps_source_contract_even_when_frozen(self):
        source_python = Path(sys.executable).resolve()
        source_root = Path(setup.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gui, console = root / "OpenComputer.exe", root / "OpenComputerCLI.exe"
            gui.touch()
            console.touch()
            with patch.object(setup.sys, "frozen", True, create=True), patch.object(setup.sys, "executable", str(gui)):
                config = setup.build_mcp_config(source_python, source_root)
                with self.assertRaisesRegex(ValueError, "Python executable"):
                    setup.build_mcp_config(root / "missing-python.exe", source_root)
                with self.assertRaisesRegex(ValueError, "Package root"):
                    setup.build_mcp_config(source_python, root / "missing-source")
            server = config["mcpServers"]["open-computer"]
            self.assertEqual(server["command"], str(source_python))
            self.assertEqual(server["args"], ["-P", "-m", "opencomputer.mcp"])
            self.assertEqual(server["env"], {"PYTHONPATH": str(source_root)})

    def test_panel_default_delegates_to_the_shared_frozen_config(self):
        from opencomputer.app import panel_mcp_config
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "OpenComputerCLI.exe"
            console.touch()
            with patch.object(setup.sys, "frozen", True, create=True), \
                    patch.object(setup.sys, "executable", str(root / "OpenComputer.exe")):
                self.assertEqual(json.loads(panel_mcp_config()), setup.build_mcp_config())

    def test_config_exports_utf8_and_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="MCP café ") as temporary:
            interpreter = Path(temporary) / "Python café.exe"
            interpreter.touch()
            output = Path(temporary) / "MCP config é.json"
            arguments = ["config", "--python", str(interpreter), "--output", str(output)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(setup.main(arguments), 0)
            original = output.read_bytes()
            self.assertIn("café".encode(), original)
            self.assertFalse(original.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.loads(original), setup.build_mcp_config(interpreter))
            self.assertIn(str(output), stdout.getvalue())
            self.assertNotIn("mcpServers", stdout.getvalue())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                setup.main(arguments)
            self.assertEqual(output.read_bytes(), original)
            output.write_text("old", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main([*arguments, "--force"]), 0)
            self.assertEqual(output.read_bytes(), original)

    def test_config_output_rejects_invalid_flags_parent_and_directory(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup.main(["config", "--force"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for output in (directory, directory / "missing" / "settings.json"):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    setup.main(["config", "--output", str(output), "--force"])
            self.assertFalse((directory / "missing").exists())

    def test_config_force_rejects_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, link = root / "target.json", root / "link.json"
            target.write_text("keep", encoding="utf-8")
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("Symlink creation unavailable")
            for force in (False, True):
                with self.assertRaises(ValueError):
                    setup.write_mcp_config({"example": True}, link, force=force)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_config_failed_force_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "settings.json"
            target.write_text("keep", encoding="utf-8")
            with patch.object(setup.os, "replace", side_effect=OSError("cannot replace")):
                with self.assertRaises(OSError):
                    setup.write_mcp_config({"example": True}, target, force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(target.parent.iterdir()), [target])

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell launcher guard")
    def test_start_launcher_missing_venv_never_falls_back_to_global_python(self):
        shell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell unavailable")
        with tempfile.TemporaryDirectory(prefix="launcher path ") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(Path(setup.__file__).resolve().parents[1] / "scripts/start.ps1", scripts / "start.ps1")
            marker = root / "global-python-called.txt"
            (root / "python.cmd").write_text(f'@echo global > "{marker}"\n', encoding="utf-8")
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(scripts / "start.ps1"), "-Demo", "-Port", "0"],
                                    env={**os.environ, "PATH": str(root) + os.pathsep + os.environ.get("PATH", "")},
                                    cwd=root, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install.cmd", result.stdout + result.stderr)
            self.assertFalse(marker.exists())

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
