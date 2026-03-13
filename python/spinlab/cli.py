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

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    p_dash.add_argument(
        "--port", type=int, default=15483, help="Dashboard port"
    )

    # lua-cmd
    p_lua = sub.add_parser("lua-cmd", help="Send raw commands to the Lua TCP server")
    p_lua.add_argument("commands", nargs="+", help="Commands to send (e.g. practice_stop reset)")

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

    elif parsed.command == "dashboard":
        import uvicorn
        import yaml
        from spinlab.dashboard import create_app
        from spinlab.db import Database

        config_path = Path(parsed.config)
        with config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        game_id = config["game"]["id"]
        data_dir = Path(config["data"]["dir"])
        host = config.get("network", {}).get("host", "127.0.0.1")
        port = config.get("network", {}).get("port", 15482)
        db = Database(data_dir / "spinlab.db")

        # Seed DB from manifest if splits are empty
        from spinlab.manifest import find_latest_manifest, load_manifest, seed_db_from_manifest
        if not db.get_active_splits(game_id):
            manifest_path = find_latest_manifest(data_dir)
            if manifest_path:
                manifest = load_manifest(manifest_path)
                seed_db_from_manifest(db, manifest, config["game"]["name"])

        app = create_app(db=db, game_id=game_id, host=host, port=port)
        print(f"SpinLab Dashboard: http://localhost:{parsed.port}")
        uvicorn.run(app, host="0.0.0.0", port=parsed.port, log_level="warning")

    elif parsed.command == "lua-cmd":
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 15482), timeout=2) as s:
                for cmd in parsed.commands:
                    s.sendall((cmd + "\n").encode())
        except OSError:
            pass  # Lua not running — nothing to do


if __name__ == "__main__":
    main()
