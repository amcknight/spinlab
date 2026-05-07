"""Tests for Vite child-process management — Job Object setup and terminate.

The integration we really want to verify (assign-then-die-and-watch-children-die)
needs a live Windows env and a real npm spawn. These tests cover the unit-level
contracts: helpers exist, return reasonable types, are idempotent, and don't
crash when called from non-Windows or with bad inputs.
"""
from __future__ import annotations

import os
import sys

import pytest

from spinlab import vite


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_kill_on_close_job_creates_handle():
    h = vite._ensure_kill_on_close_job()
    assert h is not None
    assert isinstance(h, int)
    assert h > 0


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_kill_on_close_job_is_idempotent():
    """Repeat calls return the same cached handle — we don't leak handles per call."""
    h1 = vite._ensure_kill_on_close_job()
    h2 = vite._ensure_kill_on_close_job()
    assert h1 == h2


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_assign_self_to_job_succeeds():
    """We can assign our own pid to the job (smoke check OpenProcess access rights)."""
    ok = vite._assign_pid_to_kill_on_close_job(os.getpid())
    assert ok is True


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_assign_invalid_pid_returns_false_without_raising():
    """Defensive: bad pid logs a warning and returns False; never raises."""
    # PID 999999 is unlikely to exist; OpenProcess will fail.
    ok = vite._assign_pid_to_kill_on_close_job(999_999)
    assert ok is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-op test")
def test_kill_on_close_job_returns_none_on_posix():
    """Non-Windows: helpers no-op. Job Object is a Windows-only API."""
    assert vite._ensure_kill_on_close_job() is None
    assert vite._assign_pid_to_kill_on_close_job(os.getpid()) is False


def test_terminate_vite_handles_already_dead_proc():
    """If proc is already exited, terminate_vite returns cleanly (no error)."""
    class _DeadProc:
        pid = -1
        def poll(self):
            return 0  # already exited
        def terminate(self):  # should never be called
            raise AssertionError("terminate called on dead proc")
        def kill(self):
            raise AssertionError("kill called on dead proc")

    vite.terminate_vite(_DeadProc())  # type: ignore[arg-type]
