"""Lifecycle for headless RetroArch in integration tests.

Owns:
  - subprocess.Popen of retroarch.exe with null drivers + ROM
  - NCIClient (UDP 55355) connection to the launched RA
  - RAPokeEngine bound to that client
  - graceful teardown (client.quit + Popen.terminate fallback)

NOT owned:
  - retroarch.cfg generation (harness reuses user's existing cfg per spec)
  - per-test fixture lifecycle (that's conftest.py's ra_harness fixture)
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from spinlab.retroarch.exceptions import NCITimeout
from spinlab.retroarch.nci import NCIClient
from tests.integration.ra_poke_engine import RAPokeEngine

logger = logging.getLogger(__name__)

# RA needs a moment after Popen before NCI starts replying.
NCI_PING_RETRIES = 10
NCI_PING_INTERVAL_S = 0.5

# is_core_running uses a frame-counter byte. SMW's frame counter at $0014 ticks
# every frame; same address used by scripts/smoke_nci_client.py.
ADDR_FRAME_COUNTER = 0x0014

# Teardown timing.
QUIT_GRACE_S = 2.0


class RAHarnessLaunchError(RuntimeError):
    """Raised when RA fails to launch into a usable state."""


@dataclass
class RAHarness:
    proc: subprocess.Popen
    client: NCIClient
    engine: RAPokeEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = RAPokeEngine(self.client)

    @classmethod
    def launch(
        cls,
        rom_path: Path,
        core_path: Path,
        retroarch_exe: Path,
    ) -> "RAHarness":
        for p, label in [(retroarch_exe, "retroarch_exe"), (core_path, "core_path"), (rom_path, "rom_path")]:
            if not p.exists():
                raise RAHarnessLaunchError(f"{label} does not exist: {p}")

        cmd = [
            str(retroarch_exe),
            "--video=null",
            "--audio=null",
            "-L", str(core_path),
            str(rom_path),
        ]
        logger.info("ra_harness: launching %s", cmd)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        client = NCIClient()
        # Ping until NCI replies or we exhaust retries.
        for attempt in range(NCI_PING_RETRIES):
            try:
                client.version()
                break
            except NCITimeout:
                time.sleep(NCI_PING_INTERVAL_S)
        else:
            cls._kill(proc)
            raise RAHarnessLaunchError(
                f"NCI did not reply after {NCI_PING_RETRIES} attempts × {NCI_PING_INTERVAL_S}s"
            )

        # Confirm core is running before pausing — guards against the
        # spike-found "deep pause" trap.
        if not client.is_core_running(tick_addr=ADDR_FRAME_COUNTER):
            cls._kill(proc)
            raise RAHarnessLaunchError(
                "RA NCI replied but core is not advancing frames — refusing to pause"
            )

        client.pause_toggle()
        # is_core_running with a fresh delay confirms the toggle landed.
        if client.is_core_running(tick_addr=ADDR_FRAME_COUNTER):
            cls._kill(proc)
            raise RAHarnessLaunchError("PAUSE_TOGGLE did not stop frame advance")

        return cls(proc=proc, client=client)

    def teardown(self) -> None:
        try:
            self.client.quit()
        except Exception as exc:
            logger.warning("ra_harness: client.quit() raised %s", exc)
        try:
            self.proc.wait(timeout=QUIT_GRACE_S)
        except subprocess.TimeoutExpired:
            self._kill(self.proc)
        self.client.close()

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
