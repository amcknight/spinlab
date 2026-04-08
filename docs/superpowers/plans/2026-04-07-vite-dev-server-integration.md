# Vite Dev Server Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `spinlab dashboard` auto-spawns the Vite dev server so the user gets a working UI without a second terminal command.

**Architecture:** CLI spawns `npm run dev` as a subprocess before `uvicorn.run()`, waits for port 5173 to accept connections, writes `vite_port` to `.spinlab-ports`, and passes the process handle into the FastAPI app. The lifespan context manager terminates Vite on shutdown. Static file serving is removed from `dashboard.py` since Vite handles everything.

**Tech Stack:** Python 3.11+, FastAPI, subprocess, socket, Vite/Node

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `python/spinlab/vite.py` | Create | Vite subprocess spawn, port wait, terminate |
| `python/spinlab/cli.py` | Modify | Call vite spawn, write vite_port, pass process to create_app |
| `python/spinlab/dashboard.py` | Modify | Remove static mount/middleware/root route, kill Vite in lifespan |
| `frontend/vite.config.ts` | Modify | Use `base: "/"` in dev, `base: "/static/"` in build |
| `tests/test_vite.py` | Create | Tests for spawn/wait/terminate logic |
| `tests/test_cli.py` | Modify | Test ports file includes vite_port |
| `tests/test_dashboard_integration.py` | Modify | Remove tests for static serving if any |

---

### Task 1: Update vite.config.ts base path

The current `base: "/static/"` makes Vite dev server serve assets at `/static/...`, but when the user opens `localhost:5173` directly, assets should resolve from `/`. The build still needs `/static/` for the FastAPI static mount fallback.

**Files:**
- Modify: `frontend/vite.config.ts`

- [ ] **Step 1: Update vite.config.ts to conditional base**

```ts
import { defineConfig } from "vite";

export default defineConfig(({ command }) => ({
  root: ".",
  base: command === "serve" ? "/" : "/static/",
  build: {
    outDir: "../python/spinlab/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:15483",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "happy-dom",
  },
}));
```

- [ ] **Step 2: Verify frontend tests still pass**

Run: `cd frontend && npm test`
Expected: All tests pass (base path change doesn't affect test logic)

- [ ] **Step 3: Verify build still works**

Run: `cd frontend && npm run build`
Expected: Build succeeds, outputs to `../python/spinlab/static/`

- [ ] **Step 4: Commit**

```bash
git add frontend/vite.config.ts
git commit -m "feat: use root base path for Vite dev server"
```

---

### Task 2: Create vite.py — subprocess spawn, wait, terminate

Extracted module so spawn/wait/terminate logic is testable independently of CLI and dashboard.

**Files:**
- Create: `python/spinlab/vite.py`
- Create: `tests/test_vite.py`

- [ ] **Step 1: Write failing test for `wait_for_port`**

```python
# tests/test_vite.py
"""Tests for Vite subprocess management."""
import socket
import threading
import time

import pytest

from spinlab.vite import wait_for_port


def test_wait_for_port_succeeds_when_listening():
    """wait_for_port returns True when the port accepts connections."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert wait_for_port(port, timeout=2) is True
    finally:
        srv.close()


def test_wait_for_port_fails_on_timeout():
    """wait_for_port returns False when nothing is listening."""
    # Use a port that's almost certainly not listening
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert wait_for_port(port, timeout=0.3) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vite.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.vite'`

- [ ] **Step 3: Implement wait_for_port**

```python
# python/spinlab/vite.py
"""Vite dev server subprocess management."""
from __future__ import annotations

import logging
import socket
import time

logger = logging.getLogger(__name__)

VITE_PORT = 5173
VITE_STARTUP_TIMEOUT_S = 10
VITE_POLL_INTERVAL_S = 0.25


def wait_for_port(port: int, timeout: float = VITE_STARTUP_TIMEOUT_S) -> bool:
    """Poll until *port* accepts a TCP connection, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(VITE_POLL_INTERVAL_S)
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vite.py -v`
Expected: 2 passed

- [ ] **Step 5: Write failing test for `spawn_vite`**

```python
# append to tests/test_vite.py
from unittest.mock import patch, MagicMock
from pathlib import Path

from spinlab.vite import spawn_vite, ViteStartupError


def test_spawn_vite_starts_subprocess(tmp_path):
    """spawn_vite calls Popen with the right command and cwd."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process is running

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("spinlab.vite.wait_for_port", return_value=True):
        proc = spawn_vite(tmp_path)

    assert proc is mock_proc
    call_args = mock_popen.call_args
    assert "npm" in call_args[0][0][0]
    assert str(tmp_path) == call_args[1]["cwd"]


def test_spawn_vite_raises_on_port_timeout(tmp_path):
    """spawn_vite raises ViteStartupError when port never opens."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc), \
         patch("spinlab.vite.wait_for_port", return_value=False):
        with pytest.raises(ViteStartupError, match="did not start"):
            spawn_vite(tmp_path)

    mock_proc.terminate.assert_called_once()


def test_spawn_vite_raises_on_early_exit(tmp_path):
    """spawn_vite raises ViteStartupError when process exits immediately."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # already exited
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = b"error"

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc):
        with pytest.raises(ViteStartupError, match="exited"):
            spawn_vite(tmp_path)
```

- [ ] **Step 6: Run tests to verify new tests fail**

Run: `python -m pytest tests/test_vite.py -v`
Expected: 3 new tests FAIL — `ImportError` for `spawn_vite` / `ViteStartupError`

- [ ] **Step 7: Implement spawn_vite and terminate_vite**

Add to `python/spinlab/vite.py`:

```python
import subprocess
import sys
from pathlib import Path


class ViteStartupError(RuntimeError):
    """Raised when the Vite dev server fails to start."""


def spawn_vite(frontend_dir: Path) -> subprocess.Popen:
    """Spawn ``npm run dev`` and wait for the port to accept connections.

    Raises *ViteStartupError* if the process exits early or the port
    never opens within *VITE_STARTUP_TIMEOUT_S* seconds.
    """
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    proc = subprocess.Popen(
        [npm, "run", "dev"],
        cwd=str(frontend_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Check for early exit (missing node_modules, bad config, etc.)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise ViteStartupError(
            f"Vite exited immediately (code {proc.returncode}): {stderr[:200]}"
        )
    if not wait_for_port(VITE_PORT):
        proc.terminate()
        raise ViteStartupError(
            f"Vite did not start within {VITE_STARTUP_TIMEOUT_S}s — "
            f"is port {VITE_PORT} in use?"
        )
    logger.info("Vite dev server ready on port %d", VITE_PORT)
    return proc


def terminate_vite(proc: subprocess.Popen) -> None:
    """Terminate the Vite subprocess gracefully."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("Vite dev server stopped")
```

- [ ] **Step 8: Run all vite tests**

Run: `python -m pytest tests/test_vite.py -v`
Expected: 5 passed

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/vite.py tests/test_vite.py
git commit -m "feat: add Vite subprocess spawn/wait/terminate module"
```

---

### Task 3: Wire Vite into CLI and dashboard lifecycle

**Files:**
- Modify: `python/spinlab/cli.py:93-107`
- Modify: `python/spinlab/dashboard.py:64-91` (create_app signature + lifespan)
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test — ports file includes vite_port**

```python
# append to tests/test_cli.py
from spinlab.cli import _write_ports_file


def test_ports_file_includes_vite_port(tmp_path):
    _write_ports_file(tmp_path, tcp_port=15482, dashboard_port=15483, vite_port=5173)
    content = (tmp_path / ".spinlab-ports").read_text()
    assert "vite_port=5173" in content
    assert "tcp_port=15482" in content
    assert "dashboard_port=15483" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py::test_ports_file_includes_vite_port -v`
Expected: FAIL — `TypeError: _write_ports_file() got an unexpected keyword argument 'vite_port'`

- [ ] **Step 3: Update _write_ports_file to accept vite_port**

In `python/spinlab/cli.py`, replace the `_write_ports_file` function:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py::test_ports_file_includes_vite_port -v`
Expected: PASS

- [ ] **Step 5: Update create_app to accept and store vite_process**

In `python/spinlab/dashboard.py`, change `create_app` signature and lifespan to accept and terminate the Vite process:

```python
# Add import at top
import subprocess

def create_app(
    db: Database,
    config: AppConfig | None = None,
    vite_process: subprocess.Popen | None = None,
) -> FastAPI:
```

Update the lifespan to terminate Vite on shutdown:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(event_loop(session, tcp))
        yield
        task.cancel()
        await session.shutdown()
        if vite_process is not None:
            from .vite import terminate_vite
            terminate_vite(vite_process)
```

- [ ] **Step 6: Update the dashboard command in cli.py to spawn Vite**

In `python/spinlab/cli.py`, replace the `elif parsed.command == "dashboard":` block:

```python
    elif parsed.command == "dashboard":
        import uvicorn
        from spinlab.config import AppConfig
        from spinlab.dashboard import create_app
        from spinlab.db import Database
        from spinlab.vite import spawn_vite, VITE_PORT, ViteStartupError

        config = AppConfig.from_yaml(Path(parsed.config))
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
```

- [ ] **Step 7: Run existing CLI tests to check nothing broke**

Run: `python -m pytest tests/test_cli.py -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/cli.py python/spinlab/dashboard.py tests/test_cli.py
git commit -m "feat: spawn Vite from dashboard command and write vite_port"
```

---

### Task 4: Remove static file serving from dashboard.py

**Files:**
- Modify: `python/spinlab/dashboard.py:97-138`
- Modify: `tests/test_dashboard_integration.py` (if it tests static routes)

- [ ] **Step 1: Check test_dashboard_integration.py for static-related tests**

Read `tests/test_dashboard_integration.py` and identify any tests that exercise the `/` root route, `/static/` mount, or `NoCacheStaticMiddleware`. Note their names — they'll be removed or updated.

- [ ] **Step 2: Remove static mount, middleware, root route from dashboard.py**

In `python/spinlab/dashboard.py`, remove these sections:

1. Remove the `from fastapi.staticfiles import StaticFiles` import (line 10)
2. Remove the `static_dir` variable and `/static` mount (lines 97-99)
3. Remove `from fastapi.responses import FileResponse` and `from starlette.middleware.base import BaseHTTPMiddleware` (lines 101-102)
4. Remove the entire `NoCacheStaticMiddleware` class and `app.add_middleware(...)` call (lines 104-111)
5. Remove `index_path`, the `@app.get("/")` route, and `root()` function (lines 131-137)

The resulting `create_app` after the routers section should just be:

```python
    app.include_router(practice_router)
    app.include_router(reference_router)
    app.include_router(model_router)
    app.include_router(segments_router)
    app.include_router(system_router)
    app.include_router(attempts_router)

    return app
```

- [ ] **Step 3: Remove or update static-related tests**

Remove any tests from `tests/test_dashboard_integration.py` that test:
- The root `/` route returning `index.html` or 503
- The `/static/` mount serving files
- `NoCacheStaticMiddleware` headers

- [ ] **Step 4: Run fast tests**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard_integration.py
git commit -m "refactor: remove static file serving from dashboard (Vite handles it)"
```

---

### Task 5: Run full test suite

- [ ] **Step 1: Run full pytest**

Run: `python -m pytest`
Expected: All tests pass. If any fail, fix them before proceeding.

- [ ] **Step 2: Run frontend tests and typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: All pass

- [ ] **Step 3: Final commit if any fixups were needed**

```bash
git add -u
git commit -m "fix: test fixups for Vite dev server integration"
```
