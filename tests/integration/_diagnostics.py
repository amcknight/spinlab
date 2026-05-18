"""Diagnostic capture for integration test failures.

Owns the ring-buffer logging handler (collects recent spinlab log lines)
and the formatters / collectors that the pytest hooks in conftest.py call
on failure. Pulled out of conftest so tests/unit/integration/ can import
real names rather than private conftest symbols.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import requests as http_requests

if TYPE_CHECKING:
    import pytest
    from tests.integration.ra_harness import RAHarnessLaunchError

# Tail counts. 30 is plenty to capture the RA boot sequence (~10 lines)
# plus any "core failed to load" + crash spew, without burying the report
# under thousands of frame-tick lines.
HARNESS_LOG_TAIL_LINES = 30
RING_TAIL_LINES = 30
EVENT_LOG_CAPACITY = 200


class RingHandler(logging.Handler):
    """Fixed-capacity ring buffer logging handler."""

    def __init__(self, capacity: int = EVENT_LOG_CAPACITY):
        super().__init__()
        self._buf: list[str] = []
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self._buf.append(line)
        if len(self._buf) > self._capacity:
            self._buf = self._buf[-self._capacity:]

    def recent(self, n: int = RING_TAIL_LINES) -> list[str]:
        return self._buf[-n:]

    def clear(self) -> None:
        self._buf = []


# Module-level singleton. Installed onto the spinlab logger by `install_log_handler`.
ring = RingHandler()
ring.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

_installed = False


def install_log_handler() -> None:
    """Attach `ring` to the spinlab logger exactly once per process.

    Called by `tests/integration/conftest.py` at module load. Idempotent —
    repeat calls are no-ops, so importing the module from unit tests
    doesn't double-register.
    """
    global _installed
    if _installed:
        return
    logging.getLogger("spinlab").addHandler(ring)
    _installed = True


def format_pause_toggle_failure(harness, exc: Exception) -> str:
    """Format a pause_toggle failure message for the replay fixture path.

    Pulls pid/port off the harness defensively (older test doubles may not
    have them) and includes the original exception type + message.
    """
    try:
        pid = harness.proc.pid
    except Exception:
        pid = "<unknown>"
    try:
        port = harness.client.port
    except Exception:
        port = "<unknown>"
    return (
        f"replay_ra_dashboard: pause_toggle on harness "
        f"(pid={pid}, port={port}) failed with {type(exc).__name__}: {exc}"
    )


def format_dashboard_startup_failure(
    *,
    port: int,
    attempts: int,
    interval_s: float,
    last_error: Exception | None,
    subject: str = "Fake dashboard server",
) -> str:
    """Format a dashboard startup timeout message.

    Names the bound port, elapsed wall time, and most recent error so the
    operator can tell port-occupied apart from a panicked dashboard.
    """
    elapsed = attempts * interval_s
    err_str = (
        f"{type(last_error).__name__}: {last_error}"
        if last_error else "no error captured"
    )
    return (
        f"{subject} did not start on port {port} within "
        f"{elapsed:.1f}s ({attempts} × {interval_s}s). Last error: {err_str}"
    )


def collect_diagnostics(item: "pytest.Item") -> str:
    """Best-effort snapshot of integration test state at failure time.

    Walks `item.funcargs`:
      - For tuples shaped `(str_url, Database, ...)`, emits /api/state +
        DB-counts block.
      - For objects exposing `.proc` and `.client`, emits a harness block
        with pid / port / proc.poll() and a tail of the per-launch retroarch.log.

    Always tails the in-process spinlab log ring buffer at the end.
    """
    parts: list[str] = []

    for fixture_name, fixture_val in item.funcargs.items():
        # ---- Dashboard-shaped: (base_url, db, _) ----
        if (
            isinstance(fixture_val, tuple)
            and len(fixture_val) >= 2
            and isinstance(fixture_val[0], str)
            and fixture_val[0].startswith("http")
        ):
            base_url = fixture_val[0]
            db = fixture_val[1]
            parts.append(f"  fixture: {fixture_name}")
            try:
                state = http_requests.get(f"{base_url}/api/state", timeout=2).json()
                parts.append(f"  /api/state: {json.dumps(state, indent=2)}")
            except Exception as exc:
                parts.append(f"  /api/state: <unavailable: {exc}>")
            try:
                seg_count = db.conn.execute(
                    "SELECT COUNT(*) FROM segments WHERE active = 1"
                ).fetchone()[0]
                ref_count = db.conn.execute(
                    "SELECT COUNT(*) FROM capture_runs"
                ).fetchone()[0]
                draft_count = db.conn.execute(
                    "SELECT COUNT(*) FROM capture_runs WHERE draft = 1"
                ).fetchone()[0]
                parts.append(
                    f"  DB: {seg_count} active segments, "
                    f"{ref_count} capture_runs ({draft_count} drafts)"
                )
            except Exception as exc:
                parts.append(f"  DB: <unavailable: {exc}>")
            continue

        # ---- Harness-shaped: duck-types on .proc + .client ----
        if hasattr(fixture_val, "proc") and hasattr(fixture_val, "client"):
            try:
                proc_status = fixture_val.proc.poll()
            except Exception as exc:
                proc_status = f"<poll failed: {exc}>"
            try:
                port = fixture_val.client.port
            except Exception:
                port = "<unknown>"
            try:
                pid = fixture_val.proc.pid
            except Exception:
                pid = "<unknown>"
            parts.append(
                f"  harness: {fixture_name} pid={pid} port={port} proc.poll()={proc_status}"
            )
            log_path = getattr(fixture_val, "log_path", None)
            if log_path is not None:
                try:
                    if log_path.exists():
                        text = log_path.read_text(errors="replace")
                        tail = text.splitlines()[-HARNESS_LOG_TAIL_LINES:]
                        if tail:
                            parts.append(f"  retroarch.log tail ({len(tail)} lines):")
                            for line in tail:
                                parts.append(f"    {line}")
                except Exception as exc:
                    parts.append(f"  retroarch.log: <unavailable: {exc}>")

    recent = ring.recent(RING_TAIL_LINES)
    if recent:
        parts.append(f"  Recent spinlab log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    if not parts:
        return ""
    return "\n--- SpinLab Integration Diagnostics ---\n" + "\n".join(parts)


def collect_launch_failure_diagnostics(exc: "RAHarnessLaunchError") -> str:
    """Best-effort snapshot when RAHarness.launch fails during fixture setup.

    Reads structured fields off the typed exception and tails the preserved
    retroarch.log if its `log_path` still exists. Always tails the spinlab
    logger ring at the end so the report has parity with the call-phase block.
    """
    parts: list[str] = [
        f"  RAHarnessLaunchError:"
        f" stage={exc.stage!r}"
        f" pid={exc.pid}"
        f" port={exc.port}"
        f" startup_duration_s={exc.startup_duration_s}",
    ]
    log_path = exc.log_path
    if log_path is not None:
        try:
            if log_path.exists():
                text = log_path.read_text(errors="replace")
                tail = text.splitlines()[-HARNESS_LOG_TAIL_LINES:]
                if tail:
                    parts.append(
                        f"  retroarch.log tail ({len(tail)} lines) from {log_path}:"
                    )
                    for line in tail:
                        parts.append(f"    {line}")
        except Exception as inner:
            parts.append(f"  retroarch.log: <unavailable: {inner}>")

    recent = ring.recent(RING_TAIL_LINES)
    if recent:
        parts.append(f"  Recent spinlab log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    return "\n--- SpinLab Launch-Failure Diagnostics ---\n" + "\n".join(parts)
