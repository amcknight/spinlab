"""Vite dev server subprocess management with Windows zombie-process prevention.

Vite children run inside a Windows Job Object with KILL_ON_JOB_CLOSE so the
OS terminates every descendant (npm.cmd, node.exe, …) when the dashboard
process exits, regardless of how — clean shutdown, taskkill /F, or crash.
``proc.terminate()`` alone can't do this on Windows.
"""
from __future__ import annotations

import ctypes
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

# Module-global Job Object handle. Held open for the lifetime of the dashboard
# process; the OS closes it on exit, which (because of the KILL_ON_JOB_CLOSE
# flag) auto-terminates every process assigned to the job.
_KILL_ON_CLOSE_JOB: int | None = None


class ViteStartupError(RuntimeError):
    """Raised when the Vite dev server fails to start."""


# ---- Windows Job Object plumbing ------------------------------------------

# Class index for SetInformationJobObject — see JOBOBJECTINFOCLASS.
_JobObjectExtendedLimitInformation = 9

# Flag in JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags that causes assigned
# processes to terminate when the job handle closes (i.e., when *we* exit).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# OpenProcess access rights needed for AssignProcessToJobObject:
#   PROCESS_SET_QUOTA | PROCESS_TERMINATE
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _kernel32():
    """Return a kernel32 DLL handle with the win32 fns we need argtype-declared."""
    if sys.platform != "win32":
        return None
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without explicit argtypes, ctypes guesses — and on 64-bit Windows that
    # produces "argument N: RecursionError: Stack overflow" for HANDLE/DWORD
    # mismatches. Be explicit.
    k.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    k.CreateJobObjectW.restype = ctypes.c_void_p  # HANDLE
    k.SetInformationJobObject.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    )
    k.SetInformationJobObject.restype = ctypes.c_int  # BOOL
    k.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k.OpenProcess.restype = ctypes.c_void_p  # HANDLE
    k.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    k.AssignProcessToJobObject.restype = ctypes.c_int  # BOOL
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    k.CloseHandle.restype = ctypes.c_int  # BOOL
    return k


def _ensure_kill_on_close_job() -> int | None:
    """Lazy-create the module-global Job Object on first call.

    Returns the handle (an int) on Windows, or None on non-Windows / failure.
    Idempotent — subsequent calls return the cached handle.
    """
    global _KILL_ON_CLOSE_JOB
    if _KILL_ON_CLOSE_JOB is not None:
        return _KILL_ON_CLOSE_JOB
    kernel32 = _kernel32()
    if kernel32 is None:
        return None

    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        logger.warning(
            "CreateJobObjectW failed (err=%d); Vite children may leak on hard kill",
            ctypes.get_last_error(),
        )
        return None

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        h_job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        logger.warning(
            "SetInformationJobObject failed (err=%d); closing job handle",
            ctypes.get_last_error(),
        )
        kernel32.CloseHandle(h_job)
        return None

    _KILL_ON_CLOSE_JOB = h_job
    logger.debug("Created KILL_ON_JOB_CLOSE Job Object (handle=%d)", h_job)
    return h_job


def _assign_pid_to_kill_on_close_job(pid: int) -> bool:
    """Assign *pid* to the kill-on-close Job Object. No-op on non-Windows."""
    h_job = _ensure_kill_on_close_job()
    if h_job is None:
        return False
    kernel32 = _kernel32()
    if kernel32 is None:
        return False

    h_proc = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, int(pid)
    )
    if not h_proc:
        logger.warning(
            "OpenProcess(pid=%d) failed (err=%d); Vite child not assigned to job",
            pid, ctypes.get_last_error(),
        )
        return False
    try:
        ok = bool(kernel32.AssignProcessToJobObject(h_job, h_proc))
        if not ok:
            logger.warning(
                "AssignProcessToJobObject(pid=%d) failed (err=%d); "
                "child may leak on hard kill",
                pid, ctypes.get_last_error(),
            )
        return ok
    finally:
        kernel32.CloseHandle(h_proc)


# ---- Public API -----------------------------------------------------------


def wait_for_port(port: int, timeout: float = VITE_STARTUP_TIMEOUT_S) -> bool:
    """Poll until *port* accepts a TCP connection, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                pass
        time.sleep(VITE_POLL_INTERVAL_S)
    return False


def spawn_vite(frontend_dir: Path) -> subprocess.Popen:
    """Spawn ``npm run dev`` and wait for the port to accept connections.

    On Windows the spawned process is assigned to a Job Object with
    KILL_ON_JOB_CLOSE so it dies with us regardless of how we exit. Raises
    *ViteStartupError* if the process exits early or the port never opens
    within *VITE_STARTUP_TIMEOUT_S* seconds.
    """
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    proc = subprocess.Popen(
        [npm, "run", "dev"],
        cwd=str(frontend_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise ViteStartupError(
            f"Vite exited immediately (code {proc.returncode}): {stderr[:200]}"
        )

    # Assign to the kill-on-close job *before* waiting on the port — if Vite
    # never starts the port, we still want the orphan tree cleaned up when
    # we raise the timeout exception below.
    _assign_pid_to_kill_on_close_job(proc.pid)

    if not wait_for_port(VITE_PORT):
        terminate_vite(proc)
        raise ViteStartupError(
            f"Vite did not start within {VITE_STARTUP_TIMEOUT_S}s — "
            f"is port {VITE_PORT} in use?"
        )
    logger.info("Vite dev server ready on port %d", VITE_PORT)
    return proc


def terminate_vite(proc: subprocess.Popen) -> None:
    """Terminate the Vite subprocess and its descendants.

    On Windows uses ``taskkill /T /F`` to walk the process tree because
    ``proc.terminate()`` only kills the npm.cmd parent, leaving the actual
    node.exe child running. The Job Object guarantees cleanup even if this
    path is bypassed (e.g. dashboard hard-killed mid-shutdown), but using
    taskkill here gives a faster, deterministic shutdown on the happy path.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        # Walks the process tree (npm → node → ...) and force-terminates each.
        # Hide the popup window with creationflags=CREATE_NO_WINDOW.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                check=False,
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("taskkill on Vite pid=%d failed: %s", proc.pid, exc)
            proc.kill()
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    logger.info("Vite dev server stopped")
