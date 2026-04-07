"""SpinLab CLI entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SOCKET_CONNECT_TIMEOUT_S = 2


def _write_ports_file(project_dir: Path, tcp_port: int, dashboard_port: int) -> None:
    """Write .spinlab-ports for external tools (AHK scripts, etc.)."""
    ports_file = project_dir / ".spinlab-ports"
    ports_file.write_text(
        f"tcp_port={tcp_port}\ndashboard_port={dashboard_port}\n",
        encoding="utf-8",
    )


class _StripPrefixFilter(logging.Filter):
    """Strip 'spinlab.' prefix from logger names for compact output."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.shortname = record.name.removeprefix("spinlab.")  # type: ignore[attr-defined]
        return True


def _setup_file_logging(data_dir: Path) -> None:
    """Configure rotating file log in data_dir/spinlab.log."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "spinlab.log"
    handler = RotatingFileHandler(
        str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(shortname)s — %(message)s",
        datefmt="%m-%d %H:%M:%S",
    ))
    handler.addFilter(_StripPrefixFilter())
    handler.setLevel(logging.INFO)
    logging.root.addHandler(handler)
    logging.root.setLevel(min(logging.root.level or logging.INFO, logging.INFO))
    logging.getLogger("spinlab.cli").info(
        "==== Dashboard starting %s", "=" * 40
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

    # db
    p_db = sub.add_parser("db", help="Database management commands")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    p_db_reset = db_sub.add_parser("reset", help="Delete and recreate the database")
    p_db_reset.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    parsed = parser.parse_args(args)

    if parsed.command == "stats":
        print("Stats coming in a future step.")
        sys.exit(0)

    elif parsed.command == "dashboard":
        import uvicorn
        from spinlab.config import AppConfig
        from spinlab.dashboard import create_app
        from spinlab.db import Database

        config = AppConfig.from_yaml(Path(parsed.config))
        _setup_file_logging(config.data_dir)
        dashboard_port = parsed.port or config.network.dashboard_port
        db = Database(config.data_dir / "spinlab.db")

        app = create_app(db=db, config=config)
        _write_ports_file(Path(parsed.config).parent, config.network.port, dashboard_port)
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

    elif parsed.command == "db":
        if parsed.db_command == "reset":
            from spinlab.config import AppConfig
            from spinlab.db import Database

            config = AppConfig.from_yaml(Path(parsed.config))
            db_path = config.data_dir / "spinlab.db"
            if db_path.exists():
                db_path.unlink()
            for suffix in (".db-wal", ".db-shm"):
                wal = config.data_dir / f"spinlab{suffix}"
                if wal.exists():
                    wal.unlink()
            Database(str(db_path))
            print(f"Database reset: {db_path}")


if __name__ == "__main__":
    main()
