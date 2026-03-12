"""SpinLab CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="spinlab",
        description="SpinLab — spaced repetition practice for SNES speedrunning",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # practice
    p_practice = sub.add_parser("practice", help="Start a practice session")
    p_practice.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )

    # capture
    sub.add_parser("capture", help="Process passive log into a manifest")

    # stats
    sub.add_parser("stats", help="Show practice statistics (coming soon)")

    parsed = parser.parse_args(args)

    if parsed.command == "practice":
        from spinlab import orchestrator
        orchestrator.run(Path(parsed.config))

    elif parsed.command == "capture":
        from spinlab.capture import main as capture_main
        capture_main()

    elif parsed.command == "stats":
        print("Stats coming in a future step.")
        sys.exit(0)


if __name__ == "__main__":
    main()
