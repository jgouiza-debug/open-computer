"""Generate portable MCP wiring and diagnose a local installation without input."""

from __future__ import annotations

import argparse
import importlib
from importlib import metadata
import inspect
import json
from pathlib import Path
import sys
from typing import Callable

from .mcp import LocalClient
from .server import DEFAULT_CONNECTION_PATH


def build_mcp_config(python_executable: str | Path | None = None,
                     package_root: str | Path | None = None) -> dict:
    """Return generic/Unsloth import JSON, without credentials or cwd assumptions."""
    executable = Path(python_executable or sys.executable).expanduser().resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    root = Path(package_root or Path(__file__).resolve().parent.parent).expanduser().resolve()
    if not (root / "opencomputer" / "mcp.py").is_file():
        raise ValueError("Package root does not contain opencomputer/mcp.py")
    return {"mcpServers": {"open-computer": {
        "command": str(executable), "args": ["-P", "-m", "opencomputer.mcp"],
        "env": {"PYTHONPATH": str(root)},
    }}}


def dependency_checks() -> list[dict]:
    """Import required libraries only; never create windows, load models, or capture."""
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 11),
         "detail": f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; requires 3.11+"},
        {"name": "windows", "ok": sys.platform == "win32", "detail": "Native input requires Windows"},
    ]
    try:
        image_grab = importlib.import_module("PIL.ImageGrab")
        pillow_version = metadata.version("Pillow")
        parts = pillow_version.split(".")[:3]
        version = tuple(int(part) for part in parts)
        version += (0,) * (3 - len(version))
        valid = (12, 2, 0) <= version < (13, 0, 0) and "window" in inspect.signature(image_grab.grab).parameters
        checks.append({"name": "pillow", "ok": valid,
                       "detail": f"Pillow {pillow_version}; requires supported range >=12.2,<13 with window capture"})
    except (ImportError, metadata.PackageNotFoundError, ValueError, TypeError):
        checks.append({"name": "pillow", "ok": False,
                       "detail": "Pillow >=12.2,<13 with window capture is required"})
    try:
        importlib.import_module("tkinter")
        checks.append({"name": "tkinter", "ok": True, "detail": "Tkinter is importable"})
    except ImportError:
        checks.append({"name": "tkinter", "ok": False, "detail": "Install a Python distribution that includes Tkinter"})
    return checks


def run_doctor(connection_path: str | Path | None = None, include_live: bool = True,
               client_factory: Callable[[Path], LocalClient] | None = None) -> dict:
    """Read dependencies and live GUI status, exposing neither token nor raw errors."""
    path = Path(connection_path or DEFAULT_CONNECTION_PATH)
    checks = dependency_checks()
    gui = {"checked": include_live, "credential_file_present": path.is_file(),
           "running": False, "armed": None, "stopped": None, "backend": None}
    if include_live:
        if not gui["credential_file_present"]:
            checks.append({"name": "gui", "ok": False,
                           "detail": "Start Open Computer locally, then run doctor again"})
        else:
            try:
                factory = client_factory or LocalClient.from_file
                status = factory(path).call("status")
                if not isinstance(status, dict) or type(status.get("armed")) is not bool:
                    raise ValueError("Invalid GUI status")
                gui.update(running=True, armed=status["armed"],
                           stopped=status.get("stopped") is True,
                           backend=status.get("backend") if status.get("backend") in ("windows", "demo") else "unknown")
                checks.append({"name": "gui", "ok": not status["armed"],
                               "detail": "GUI is running and disarmed" if not status["armed"] else
                               "GUI is armed; disarm locally before checking safe readiness"})
            except Exception:
                # Transport/config errors may contain credentials. Report a
                # fixed diagnosis; the user can regenerate the GUI connection.
                checks.append({"name": "gui", "ok": False,
                               "detail": "GUI health could not be verified; start the GUI and retry"})
    return {"ok": all(check["ok"] for check in checks), "checks": checks, "gui": gui}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config_parser = commands.add_parser("config", help="Print credential-free MCP JSON")
    config_parser.add_argument("--python", type=Path, dest="python_executable")
    config_parser.add_argument("--format", choices=("generic", "unsloth"), default="generic",
                               help="Both formats use the portable mcpServers object")
    doctor_parser = commands.add_parser("doctor", help="Check dependencies and live disarmed GUI")
    doctor_parser.add_argument("--offline", action="store_true", help="Check dependencies without contacting the GUI")
    doctor_parser.add_argument("--connection", type=Path, default=DEFAULT_CONNECTION_PATH)
    options = parser.parse_args(argv)
    if options.command == "config":
        try:
            result = build_mcp_config(options.python_executable)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))
        return 0
    result = run_doctor(options.connection, include_live=not options.offline)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
