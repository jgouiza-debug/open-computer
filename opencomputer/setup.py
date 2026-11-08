"""Generate portable MCP wiring and diagnose a local installation without input."""

from __future__ import annotations

import argparse
import importlib
from importlib import metadata
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable

from .mcp import LocalClient
from .server import DEFAULT_CONNECTION_PATH


def validate_native_helper(path: str | Path) -> Path:
    """Validate only an explicitly supplied local file; never search or execute."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Native helper must be an absolute path")
    candidate = candidate.resolve()
    if candidate.suffix.casefold() != ".exe" or not candidate.is_file():
        raise ValueError("Native helper must be an existing .exe file")
    return candidate


def build_mcp_config(python_executable: str | Path | None = None,
                     package_root: str | Path | None = None) -> dict:
    """Return generic/Unsloth import JSON, without credentials or cwd assumptions."""
    executable = Path(python_executable or sys.executable).expanduser().resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    if executable.name.casefold() == "pythonw.exe":
        executable = executable.with_name("python.exe")
        if not executable.is_file():
            raise ValueError("Console python.exe must exist beside pythonw.exe for MCP stdio")
    root = Path(package_root or Path(__file__).resolve().parent.parent).expanduser().resolve()
    if not (root / "opencomputer" / "mcp.py").is_file():
        raise ValueError("Package root does not contain opencomputer/mcp.py")
    return {"mcpServers": {"open-computer": {
        "command": str(executable), "args": ["-P", "-m", "opencomputer.mcp"],
        "env": {"PYTHONPATH": str(root)},
    }}}


def write_mcp_config(config: dict, path: str | Path, *, force: bool = False) -> Path:
    """Export only to an explicit file; never merge or follow an output symlink."""
    destination = Path(path).expanduser().absolute()
    if destination.is_symlink() or destination.is_dir():
        raise ValueError("Output must be a regular file path, not a directory or symlink")
    if not destination.parent.is_dir():
        raise ValueError("Output directory does not exist; create it first")
    text = json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if not force:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
                                         prefix=".opencomputer-config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if destination.is_symlink() or destination.is_dir():
            raise ValueError("Output became a directory or symlink; refusing to replace it")
        # Replacing the directory entry never writes through a swapped symlink.
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


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
               client_factory: Callable[[Path], LocalClient] | None = None,
               native_helper: str | Path | None = None) -> dict:
    """Read dependencies and live GUI status, exposing neither token nor raw errors."""
    path = Path(connection_path or DEFAULT_CONNECTION_PATH)
    checks = dependency_checks()
    if native_helper is not None:
        try:
            validate_native_helper(native_helper)
            checks.append({"name": "native_helper_file", "ok": True,
                           "detail": "Explicit helper file exists; protocol not checked or executed"})
        except (ValueError, OSError):
            checks.append({"name": "native_helper_file", "ok": False,
                           "detail": "Supply an absolute path to an existing native helper .exe"})
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
    config_parser.add_argument("--output", type=Path, help="Write UTF-8 JSON to this file; its parent must exist")
    config_parser.add_argument("--force", action="store_true", help="Explicitly replace an existing output file")
    doctor_parser = commands.add_parser("doctor", help="Check dependencies and live disarmed GUI")
    doctor_parser.add_argument("--offline", action="store_true", help="Check dependencies without contacting the GUI")
    doctor_parser.add_argument("--connection", type=Path, default=DEFAULT_CONNECTION_PATH)
    doctor_parser.add_argument("--native-helper", type=Path,
                               help="Check an explicit helper file only; never launch it")
    options = parser.parse_args(argv)
    if options.command == "config":
        if options.force and options.output is None:
            parser.error("--force requires --output")
        try:
            result = build_mcp_config(options.python_executable)
            if options.output is not None:
                destination = write_mcp_config(result, options.output, force=options.force)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if options.output is not None:
            print(f"Wrote MCP config to {destination}")
            return 0
        print(json.dumps(result, indent=2))
        return 0
    result = run_doctor(options.connection, include_live=not options.offline,
                        native_helper=options.native_helper)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
