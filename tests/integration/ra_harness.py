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
import tempfile
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

# Bytes of low-WRAM read after FRAMEADVANCE to verify the core actually advanced.
# Must cover at least WRAM[0x13] (byte index 19) — the standard SMW global frame
# counter, which ticks every frame.  16 bytes missed this on Love Yourself; 32
# comfortably includes it while remaining bandwidth-light.
WRAM_SANITY_PROBE_BYTES = 32

# Teardown timing.
QUIT_GRACE_S = 2.0

# Null-driver appendconfig content — suppresses video and audio output so
# tests run headless. RA 1.22.2 does not accept --video=null style CLI flags;
# --appendconfig is the correct way to override driver settings.
_NULL_DRIVER_CFG = """\
video_driver = "null"
audio_driver = "null"
"""


class RAHarnessLaunchError(RuntimeError):
    """Raised when RA fails to launch into a usable state."""


@dataclass
class RAHarness:
    proc: subprocess.Popen
    client: NCIClient
    _tmp_cfg: Path | None = field(default=None, repr=False)
    engine: RAPokeEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = RAPokeEngine(self.client)

    @classmethod
    def launch(
        cls,
        rom_path: Path,
        core_path: Path,
        retroarch_exe: Path,
        extra_cfg: str = "",
    ) -> "RAHarness":
        """Launch RetroArch headless with the given ROM and core.

        Args:
            rom_path: Path to the ROM file to load.
            core_path: Path to the libretro core (.dll/.so).
            retroarch_exe: Path to the retroarch executable.
            extra_cfg: Additional retroarch.cfg key=value pairs to append to the
                null-driver appendconfig. Used to override settings like
                savestate_directory for tests that need an isolated save dir.
        """
        for p, label in [(retroarch_exe, "retroarch_exe"), (core_path, "core_path"), (rom_path, "rom_path")]:
            if not p.exists():
                raise RAHarnessLaunchError(f"{label} does not exist: {p}")

        # Write a temporary appendconfig to enable null drivers.
        # --appendconfig keys override the user's retroarch.cfg, so the main
        # config's NCI settings (network_cmd_enable, network_cmd_port) are
        # preserved while video/audio are suppressed.
        tmp_cfg_fd, tmp_cfg_path_str = tempfile.mkstemp(suffix=".cfg", prefix="spinlab_ra_null_")
        tmp_cfg_path = Path(tmp_cfg_path_str)
        try:
            with open(tmp_cfg_fd, "w") as f:
                f.write(_NULL_DRIVER_CFG)
                if extra_cfg:
                    f.write(extra_cfg)
        except Exception:
            tmp_cfg_path.unlink(missing_ok=True)
            raise

        cmd = [
            str(retroarch_exe),
            f"--appendconfig={tmp_cfg_path}",
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
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(
                f"NCI did not reply after {NCI_PING_RETRIES} attempts × {NCI_PING_INTERVAL_S}s"
            )

        # Bring RA to a known paused state so FRAMEADVANCE is the only
        # thing that advances the core.
        #
        # RA may launch already paused (common with null video driver —
        # no display output triggers an auto-pause) or in PLAYING state.
        # We detect via GET_STATUS and act accordingly:
        #
        #   PAUSED   → already where we want to be; nothing to do.
        #   PLAYING  → pause it, then verify the toggle took effect.
        #   other    → unexpected; abort.
        #
        # We do NOT use is_core_running() here because that method detects
        # free-running frames and will incorrectly report False for a RA
        # that is paused but FRAMEADVANCE-capable (which is the correct
        # harness state).
        try:
            status = client.get_status()
        except Exception as exc:
            cls._kill(proc)
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(f"GET_STATUS failed: {exc}") from exc

        if status.state == "PAUSED":
            # Already paused — correct state for FRAMEADVANCE-driven tests.
            pass
        elif status.state == "PLAYING":
            client.pause_toggle()
            # Brief delay so the toggle takes effect before we verify.
            time.sleep(NCI_PING_INTERVAL_S)
            try:
                after = client.get_status()
            except Exception as exc:
                cls._kill(proc)
                tmp_cfg_path.unlink(missing_ok=True)
                raise RAHarnessLaunchError(f"GET_STATUS after pause_toggle failed: {exc}") from exc
            if after.state != "PAUSED":
                cls._kill(proc)
                tmp_cfg_path.unlink(missing_ok=True)
                raise RAHarnessLaunchError(
                    f"PAUSE_TOGGLE did not pause RA (status={after.state!r})"
                )
        else:
            cls._kill(proc)
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(
                f"Unexpected RA status after launch: {status.state!r} — expected PAUSED or PLAYING"
            )

        # Final sanity: confirm FRAMEADVANCE actually advances the core.
        # Read any WRAM byte, advance one frame, re-read — some byte must change.
        snap_before = client.read_ram(0x0000, WRAM_SANITY_PROBE_BYTES)
        client.frame_advance()
        time.sleep(NCI_PING_INTERVAL_S)
        snap_after = client.read_ram(0x0000, WRAM_SANITY_PROBE_BYTES)
        if snap_before == snap_after:
            cls._kill(proc)
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(
                "FRAMEADVANCE did not change any WRAM byte — core may be in deep-freeze"
            )

        return cls(proc=proc, client=client, _tmp_cfg=tmp_cfg_path)

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
        if self._tmp_cfg is not None:
            self._tmp_cfg.unlink(missing_ok=True)
            self._tmp_cfg = None

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
