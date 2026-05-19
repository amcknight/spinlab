"""DashboardHarness — context manager that owns the lifecycle of a FastAPI
dashboard wired to an in-process backend, for use by integration fixtures
and unit tests that need a real HTTP surface.

Replaces the two near-identical tmpdir+uvicorn+wait-for-ready blocks in
`fake_dashboard_server` (FakeEmuBackend) and `replay_ra_dashboard` (real
RA backend) — callers pass an AppConfig and an optional fake_emu flag.
"""
from __future__ import annotations

import shutil
import socket
import tempfile
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import requests as http_requests
import uvicorn
from tests.integration._wait_for import wait_for

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.db import Database

if TYPE_CHECKING:
    from spinlab.session_manager import SessionManager


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class DashboardContext:
    """What a `with DashboardHarness(...) as ctx:` block sees."""

    base_url: str
    db: Database
    session: SessionManager
    tmp_path: Path


def _status_200(resp: http_requests.Response) -> tuple[bool, str]:
    if resp.status_code == 200:
        return True, ""
    return False, f"HTTP {resp.status_code}"


class DashboardHarness(AbstractContextManager):
    """Owns the tmpdir + Database + uvicorn-thread lifecycle for a dashboard.

    Two construction paths:
      - `DashboardHarness(config=..., fake_emu=False)` — caller-supplied
        AppConfig. The dashboard's event_loop will try to connect to
        whatever backend the config points at.
      - `DashboardHarness.fake(tmp_path_root=...)` — builds an AppConfig
        with throwaway ports and swaps in a FakeEmuBackend. The event_loop
        keeps failing to connect to nothing, which is fine for HTTP-only
        contract tests.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        fake_emu: bool = False,
        tmp_path: Path,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._config = config
        self._fake_emu = fake_emu
        self._tmp_path = tmp_path
        self._startup_timeout_s = startup_timeout_s
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._db: Database | None = None
        self._app = None

    @classmethod
    def fake(
        cls, *, tmp_path_root: Path, startup_timeout_s: float = 10.0,
    ) -> "DashboardHarness":
        """Construct a fake-backed harness with throwaway config.

        The dashboard port is picked from the free range. NetworkConfig.port
        is a free port that nothing will bind to, so the event_loop's
        connect-retries fail fast.
        """
        tmp = Path(tempfile.mkdtemp(prefix="spinlab_fake_", dir=tmp_path_root))
        dashboard_port = _free_port()
        fake_tcp_port = _free_port()
        config = AppConfig(
            network=NetworkConfig(
                host="127.0.0.1",
                port=fake_tcp_port,
                dashboard_port=dashboard_port,
            ),
            emulator=EmulatorConfig(
                savestate_dir=tmp / "ra",
                spinlab_state_dir=tmp / "sl",
            ),
            data_dir=tmp,
            rom_dir=None,
        )
        return cls(
            config=config, fake_emu=True, tmp_path=tmp,
            startup_timeout_s=startup_timeout_s,
        )

    def __enter__(self) -> DashboardContext:
        from spinlab.dashboard import create_app

        self._db = Database(str(self._tmp_path / "spinlab.db"))
        self._app = create_app(db=self._db, config=self._config)

        if self._fake_emu:
            from tests.conftest import FakeEmuBackend
            fake_emu_backend = FakeEmuBackend(connected=True)
            self._app.state.session.emu = fake_emu_backend
            self._app.state.session.capture.emu = fake_emu_backend
            self._app.state.session.cold_fill.emu = fake_emu_backend

        uvi_config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self._config.network.dashboard_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uvi_config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        base_url = f"http://127.0.0.1:{self._config.network.dashboard_port}"

        outcome = wait_for(
            name="dashboard_ready",
            fetch=lambda: http_requests.get(f"{base_url}/api/state", timeout=1.0),
            predicate=_status_200,
            timeout_s=self._startup_timeout_s,
            interval_s=0.25,
        )
        if not outcome.succeeded:
            self._teardown()
            raise RuntimeError(outcome.format_message())

        return DashboardContext(
            base_url=base_url,
            db=self._db,
            session=self._app.state.session,
            tmp_path=self._tmp_path,
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # uvicorn didn't drain — surface so the next test's "address
                # in use" failure isn't a mystery. daemon=True keeps the
                # thread from blocking process exit, so warn rather than raise.
                import warnings
                warnings.warn(
                    "DashboardHarness: uvicorn thread did not stop within 5s",
                    stacklevel=2,
                )
        if self._db is not None:
            self._db.close()
        shutil.rmtree(self._tmp_path, ignore_errors=True)
