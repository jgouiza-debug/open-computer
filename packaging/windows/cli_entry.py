"""Console entry: the mcp subcommand owns stdout exclusively."""
from opencomputer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
