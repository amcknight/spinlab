"""Dashboard boot wires the JAX prewarm in a background thread."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock


def test_prewarm_dispatched_in_background_thread(monkeypatch):
    """Boot should spawn a daemon thread that calls _prewarm_segments_model,
    never blocking the main startup."""
    from spinlab import dashboard

    done = threading.Event()
    spy = MagicMock(side_effect=lambda: done.set())
    monkeypatch.setattr(dashboard, "_prewarm_segments_model", spy)

    dashboard._run_startup_hooks()

    # The thread is daemon + parallel; wait briefly for it to fire.
    assert done.wait(timeout=2.0), "prewarm thread did not run within 2s"
    spy.assert_called_once()


def test_prewarm_failure_does_not_crash_boot(monkeypatch):
    """Missing [fits] extra (or any other prewarm exception) must not
    propagate out of _run_startup_hooks and crash dashboard boot."""
    from spinlab import dashboard

    def boom():
        raise ImportError("jax not installed")
    monkeypatch.setattr(dashboard, "_prewarm_segments_model", boom)

    # _run_startup_hooks spawns the thread synchronously; any exception
    # raised inside the thread target is the thread's problem, not the
    # caller's. The call itself must return cleanly.
    dashboard._run_startup_hooks()
    # Give the daemon thread a moment to die.
    time.sleep(0.1)


def test_prewarm_segments_model_handles_missing_extra(monkeypatch, caplog):
    """When [fits] isn't installed, _prewarm_segments_model logs a WARNING
    with an actionable install hint and returns cleanly.

    Warning (not info) because the user otherwise has no signal that the
    silent fit pipeline is disabled — a green dashboard with empty
    `segment_fits` looks identical whether [fits] is missing or the user
    simply hasn't accumulated n>=5 attempts yet.
    """
    from spinlab import dashboard

    # Force the in-function `from spinlab.segments_model import prewarm_buckets`
    # to raise ImportError by stubbing the module attribute lookup. The
    # simplest reliable approach: monkeypatch sys.modules so the import
    # inside the function fails.
    import sys
    monkeypatch.setitem(sys.modules, "spinlab.segments_model", None)

    with caplog.at_level("WARNING", logger=dashboard.__name__):
        dashboard._prewarm_segments_model()

    # No exception escaped; a warning-level log mentioning the [fits] extra
    # install hint was emitted.
    matching = [r for r in caplog.records if r.levelname == "WARNING"]
    assert matching, f"expected WARNING log, got {[(r.levelname, r.message) for r in caplog.records]}"
    assert any("[fits]" in r.message and "pip install" in r.message for r in matching), (
        f"expected install hint, got messages: {[r.message for r in matching]}"
    )


def test_prewarm_segments_model_logs_exceptions(monkeypatch, caplog):
    """If prewarm_buckets itself blows up, log at exception level and
    return — don't propagate the failure to the thread / process."""
    from spinlab import dashboard

    import sys
    import types
    fake_mod = types.ModuleType("spinlab.segments_model")

    def boom():
        raise RuntimeError("prewarm exploded")
    fake_mod.prewarm_buckets = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spinlab.segments_model", fake_mod)

    with caplog.at_level("ERROR", logger=dashboard.__name__):
        dashboard._prewarm_segments_model()

    assert any(
        "prewarm failed" in rec.message for rec in caplog.records
    )
