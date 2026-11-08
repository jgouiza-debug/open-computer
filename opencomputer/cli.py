"""Console entry point shared by source installs and the portable runtime."""

from __future__ import annotations

import argparse
import importlib
import sys

COMMANDS = {"mcp": ".mcp", "setup": ".setup", "run": ".model_client"}


def main(argv: list[str] | None = None) -> int:
    """Dispatch lazily without printing into an MCP protocol stream."""
    # Frozen console pipes can default to ANSI; paths and MCP use UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=tuple(COMMANDS),
                        help="mcp: stdio bridge; setup: config/doctor; run: existing local model client")
    # Parse only the command: a subcommand's --help/options belong to its parser.
    command = parser.parse_args(arguments[:1]).command
    module = importlib.import_module(COMMANDS[command], __package__)
    result = module.main(arguments[1:])
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())
