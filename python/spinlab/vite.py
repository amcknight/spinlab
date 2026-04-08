"""Vite dev server subprocess management."""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

VITE_PORT = 5173
VITE_STARTUP_TIMEOUT_S = 10
VITE_POLL_INTERVAL_S = 0.25


class ViteStartupError(RuntimeError):
    """Raised when the Vite dev server fails to start."""


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
