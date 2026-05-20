"""SpinLab CLI entry point."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.config import AppConfig

# Where startup errors land when the regular file logger isn't configured yet
# (e.g. config-not-found before _setup_file_logging runs). AHK launches the
# dashboard minimized; without this, any pre-logger crash vanishes from view.
_STARTUP_ERROR_DIR = Path(tempfile.gettempdir())


def _write_startup_error(exc: BaseException, context: str) -> Path:
    """Dump an unhandled startup exception to a stable temp path.

    Returns the file path so the caller can echo it. Best-effort — never
    raises; if even the temp write fails, we swallow it (we're already in
    an error path).
    """
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = _STARTUP_ERROR_DIR / f"spinlab-startup-{ts}-{os.getpid()}.log"
        with path.open("w", encoding="utf-8") as f:
            f.write(f"[spinlab startup failure] {context}\n")
            f.write(f"timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"cwd: {Path.cwd()}\n")
            f.write(f"argv: {sys.argv!r}\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        return path
    except Exception:
        return _STARTUP_ERROR_DIR / "spinlab-startup-write-failed"


def _load_config_or_die(config_path_str: str) -> AppConfig:
    """Load the YAML config or print an actionable error and exit.

    The default `config.yaml` (relative path) is resolved against CWD. When the
    dashboard is launched detached (e.g. AHK minimized window), a missing config
    file would otherwise produce a stack trace that scrolls past invisibly. This
    wrapper makes the failure mode self-explanatory regardless of how the
    process was started.
    """
    from spinlab.config import AppConfig

    config_path = Path(config_path_str)
    abs_path = config_path.resolve()
    if not config_path.exists():
        msg = (
            f"Config not found: {abs_path}\n"
            f"  --config was: {config_path_str!r}\n"
            f"  resolved to:  {abs_path}\n"
            f"  cwd:          {Path.cwd()}\n"
            f"\nFix: either pass an existing config path with --config, or copy "
            f"config.example.yaml to config.yaml in the directory you're running "
            f"from."
        )
        print(msg, file=sys.stderr)
        # Also write a startup-error file for AHK / detached launches.
        err_file = _write_startup_error(
            FileNotFoundError(msg), "config not found"
        )
        print(f"Details also written to: {err_file}", file=sys.stderr)
        sys.exit(2)
    return AppConfig.from_yaml(config_path)


def _write_ports_file(
    project_dir: Path, tcp_port: int, dashboard_port: int, vite_port: int = 0,
) -> None:
    """Write .spinlab-ports for external tools (AHK scripts, etc.)."""
    ports_file = project_dir / ".spinlab-ports"
    lines = [
        f"tcp_port={tcp_port}",
        f"dashboard_port={dashboard_port}",
    ]
    if vite_port:
        lines.append(f"vite_port={vite_port}")
    ports_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def _run_dashboard(parsed: argparse.Namespace) -> None:
    """Dashboard startup body. Wrapped by main() with a startup-error catcher."""
    import uvicorn

    from spinlab.dashboard import create_app
    from spinlab.db import Database
    from spinlab.vite import VITE_PORT, ViteStartupError, spawn_vite

    config = _load_config_or_die(parsed.config)
    _setup_file_logging(config.data_dir)
    dashboard_port = parsed.port or config.network.dashboard_port
    db = Database(config.data_dir / "spinlab.db")

    # Resolve frontend dir relative to the package
    frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    try:
        vite_proc = spawn_vite(frontend_dir)
    except ViteStartupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    app = create_app(db=db, config=config, vite_process=vite_proc)
    _write_ports_file(
        Path(parsed.config).parent,
        config.network.port, dashboard_port, vite_port=VITE_PORT,
    )
    print(f"SpinLab Dashboard: http://localhost:{VITE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=dashboard_port, log_level="warning")


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="spinlab",
        description="SpinLab — spaced repetition practice for SNES speedrunning",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    p_dash.add_argument(
        "--port", type=int, default=None, help="Dashboard port (overrides config)"
    )

    p_replay = sub.add_parser("replay", help="Trigger replay of a .replay file via the dashboard")
    p_replay.add_argument("path", help="Path to .replay file (only the stem is used as ref_id)")
    p_replay.add_argument("--speed", type=int, default=0, help="Emulation speed (0=max, 100=normal)")
    p_replay.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_replay.add_argument("--port", type=int, default=None, help="Dashboard port (overrides config)")

    p_db = sub.add_parser("db", help="Database management commands")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    p_db_reset = db_sub.add_parser("reset", help="Delete and recreate the database")
    p_db_reset.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    # Register the segments-v07 manual EB pool refit. Lives in its own
    # module so the JAX import stays lazy until `spinlab fit-pool` runs.
    from spinlab import cli_fit_pool
    cli_fit_pool.add_subparser(sub)

    # Register the segments-v07 fit inspector (Phase 2a). Read-only over
    # segment_fits; intentionally does not import segments_model so it
    # stays usable without the [fits] extra.
    from spinlab import cli_fit
    cli_fit.add_subparser(sub)

    parsed = parser.parse_args(args)

    if parsed.command == "dashboard":
        try:
            _run_dashboard(parsed)
        except SystemExit:
            raise
        except BaseException as exc:
            err_file = _write_startup_error(exc, "dashboard startup")
            print(
                f"\n[spinlab] dashboard failed to start. Details: {err_file}",
                file=sys.stderr,
            )
            raise

    elif parsed.command == "replay":
        import requests
        import yaml
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

    elif parsed.command == "fit-pool":
        from spinlab import cli_fit_pool

        sys.exit(cli_fit_pool.run(parsed))

    elif parsed.command == "fit":
        from spinlab import cli_fit
        if parsed.fit_command == "show":
            sys.exit(cli_fit.run_show(parsed))
        elif parsed.fit_command == "list":
            sys.exit(cli_fit.run_list(parsed))
        elif parsed.fit_command == "inventory":
            from spinlab import cli_fit_inventory
            sys.exit(cli_fit_inventory.run(parsed))
        elif parsed.fit_command == "rebuild":
            from spinlab import cli_fit_rebuild
            sys.exit(cli_fit_rebuild.run(parsed))


if __name__ == "__main__":
    main()
