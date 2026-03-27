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

    # replay
    p_replay = sub.add_parser("replay", help="Replay a .spinrec file to regenerate a reference run")
    p_replay.add_argument("path", help="Path to .spinrec file")
    p_replay.add_argument("--speed", type=int, default=0, help="Emulation speed (0=max, 100=normal)")
    p_replay.add_argument("--port", type=int, default=15483, help="Dashboard port")

    # lua-cmd
    p_lua = sub.add_parser("lua-cmd", help="Send raw commands to the Lua TCP server")
    p_lua.add_argument("commands", nargs="+", help="Commands to send (e.g. practice_stop reset)")

    parsed = parser.parse_args(args)

    if parsed.command == "stats":
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
        data_dir = Path(config["data"]["dir"])
        host = config.get("network", {}).get("host", "127.0.0.1")
        port = config.get("network", {}).get("port", 15482)
        rom_dir_str = config.get("rom", {}).get("dir", "")
        rom_dir = Path(rom_dir_str) if rom_dir_str else None
        default_category = config.get("game", {}).get("category", "any%")
        db = Database(data_dir / "spinlab.db")

        app = create_app(
            db=db,
            rom_dir=rom_dir,
            host=host,
            port=port,
            config=config,
            default_category=default_category,
        )
        print(f"SpinLab Dashboard: http://localhost:{parsed.port}")
        uvicorn.run(app, host="0.0.0.0", port=parsed.port, log_level="warning")

    elif parsed.command == "replay":
        import requests
        ref_id = Path(parsed.path).stem
        resp = requests.post(
            f"http://127.0.0.1:{parsed.port}/api/replay/start",
            json={"ref_id": ref_id, "speed": parsed.speed},
        )
        print(resp.json())

    elif parsed.command == "lua-cmd":
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 15482), timeout=2) as s:
                for cmd in parsed.commands:
                    s.sendall((cmd + "\n").encode())
        except OSError:
            pass


if __name__ == "__main__":
    main()
