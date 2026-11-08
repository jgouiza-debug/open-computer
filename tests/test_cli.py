"""Source dispatcher/portable-config contracts; no frozen binary or GUI is run."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from opencomputer import cli


class CliTests(unittest.TestCase):
    def test_dispatch_forwards_options_and_exit_codes_without_stdout(self):
        for command, module_name, forwarded, result in (
            ("mcp", ".mcp", ["--connection", "C:/Folder café/connection.json"], None),
            ("setup", ".setup", ["config", "--output", "MCP config.json"], 7),
            ("run", ".model_client", ["Read only", "--model", "existing", "--observation-mode", "semantic"], None),
        ):
            entry = Mock(return_value=result)
            stdout = io.StringIO()
            with patch.object(cli.importlib, "import_module", return_value=SimpleNamespace(main=entry)) as imports, \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([command, *forwarded]), 0 if result is None else result)
            imports.assert_called_once_with(module_name, "opencomputer")
            entry.assert_called_once_with(forwarded)
            self.assertEqual(stdout.getvalue(), "")

    def test_command_help_and_parser_exit_belong_to_selected_module(self):
        entry = Mock(side_effect=SystemExit(3))
        with patch.object(cli.importlib, "import_module", return_value=SimpleNamespace(main=entry)):
            with self.assertRaises(SystemExit) as result:
                cli.main(["mcp", "--help"])
        entry.assert_called_once_with(["--help"])
        self.assertEqual(result.exception.code, 3)

    def test_root_help_and_invalid_commands_import_no_runtime(self):
        with patch.object(cli.importlib, "import_module") as imports:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
                cli.main(["--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertTrue(all(command in stdout.getvalue() for command in ("mcp", "setup", "run")))
            for arguments in ([], ["unknown"]):
                stdout = io.StringIO()
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as result:
                        cli.main(arguments)
                self.assertEqual(result.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
            imports.assert_not_called()

    def test_default_argv_dispatch_and_lazy_module_import_in_fresh_process(self):
        root = Path(__file__).resolve().parents[1]
        entry = Mock(return_value=0)
        with patch.object(cli.sys, "argv", ["OpenComputerCLI.exe", "setup", "doctor", "--offline"]), \
                patch.object(cli.importlib, "import_module", return_value=SimpleNamespace(main=entry)):
            self.assertEqual(cli.main(), 0)
        entry.assert_called_once_with(["doctor", "--offline"])
        probe = ("import json,sys; import opencomputer.cli; "
                 "print(json.dumps({name:name in sys.modules for name in "
                 "['opencomputer.app','opencomputer.mcp','opencomputer.setup',"
                 "'opencomputer.model_client','tkinter','PIL.ImageGrab']}))")
        result = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-P", "-c", probe],
            env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(json.loads(result.stdout).values()))

    def test_source_mcp_command_is_jsonrpc_only_and_accepts_forwarded_connection(self):
        root = Path(__file__).resolve().parents[1]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status"}}]
        with tempfile.TemporaryDirectory(prefix="CLI unrelated café ") as temporary:
            connection = Path(temporary) / "missing-connection.json"
            result = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-P", "-m", "opencomputer.cli",
                "mcp", "--connection", str(connection)], input="".join(json.dumps(message) + "\n" for message in messages),
                cwd=temporary, env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"},
                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "open-computer")
        self.assertEqual(replies[1]["result"], {})
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("connection_unavailable", replies[2]["result"]["content"][0]["text"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
