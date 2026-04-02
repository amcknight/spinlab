"""SpinLab CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SOCKET_CONNECT_TIMEOUT_S = 2


def _write_ports_file(project_dir: Path, tcp_port: int, dashboard_port: int) -> None:
    """Write .spinlab-ports for external tools (AHK scripts, etc.)."""
    ports_file = project_dir / ".spinlab-ports"
    ports_file.write_text(
        f"tcp_port={tcp_port}\ndashboard_port={dashboard_port}\n",
        encoding="utf-8",
    )


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
        "--port", type=int, default=None, help="Dashboard port (overrides config)"
    )

    # replay
    p_replay = sub.add_parser("replay", help="Replay a .spinrec file to regenerate a reference run")
    p_replay.add_argument("path", help="Path to .spinrec file")
    p_replay.add_argument("--speed", type=int, default=0, help="Emulation speed (0=max, 100=normal)")
    p_replay.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_replay.add_argument("--port", type=int, default=None, help="Dashboard port (overrides config)")

    # lua-cmd
    p_lua = sub.add_parser("lua-cmd", help="Send raw commands to the Lua TCP server")
    p_lua.add_argument("commands", nargs="+", help="Commands to send (e.g. practice_stop reset)")
    p_lua.add_argument("--config", default="config.yaml", help="Path to config.yaml")

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
        network = config.get("network", {})
        data_dir = Path(config["data"]["dir"])
        host = network.get("host", "127.0.0.1")
        tcp_port = network.get("port", 15482)
        dashboard_port = parsed.port or network.get("dashboard_port", 15483)
        rom_dir_str = config.get("rom", {}).get("dir", "")
        rom_dir = Path(rom_dir_str) if rom_dir_str else None
        default_category = config.get("game", {}).get("category", "any%")
        db = Database(data_dir / "spinlab.db")

        app = create_app(
            db=db,
            rom_dir=rom_dir,
            host=host,
            port=tcp_port,
            config=config,
            default_category=default_category,
        )
        # Write ports file for external tools (AHK, scripts)
        _write_ports_file(config_path.parent, tcp_port, dashboard_port)
        print(f"SpinLab Dashboard: http://localhost:{dashboard_port}")
        uvicorn.run(app, host="0.0.0.0", port=dashboard_port, log_level="warning")

    elif parsed.command == "replay":
        import yaml
        import requests
        config_path = Path(parsed.config)
        with config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        network = config.get("network", {})
        dashboard_port = parsed.port or network.get("dashboard_port", 15483)
        ref_id = Path(parsed.path).stem
        resp = requests.post(
            f"http://127.0.0.1:{dashboard_port}/api/replay/start",
            json={"ref_id": ref_id, "speed": parsed.speed},
        )
        print(resp.json())

    elif parsed.command == "lua-cmd":
        import socket
        import yaml
        config_path = Path(parsed.config)
        with config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        network = config.get("network", {})
        tcp_host = network.get("host", "127.0.0.1")
        tcp_port = network.get("port", 15482)
        try:
            with socket.create_connection((tcp_host, tcp_port), timeout=SOCKET_CONNECT_TIMEOUT_S) as s:
                for cmd in parsed.commands:
                    s.sendall((cmd + "\n").encode())
        except OSError:
            pass


if __name__ == "__main__":
    main()
