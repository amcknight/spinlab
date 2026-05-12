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
from spinlab.retroarch.nci import DEFAULT_PORT, NCIClient
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

# Sanity-probe retry config. RA occasionally launches in a state where the
# first FRAMEADVANCE after PAUSE_TOGGLE is a no-op (likely runahead / save-
# buffer warm-up). Retrying a few times gets past the warm-up without
# masking a true deep-freeze (where no advance ever changes WRAM).
WRAM_SANITY_RETRIES = 5
WRAM_SANITY_RETRY_DELAY_S = 0.3

# PAUSE_TOGGLE → status check race. Under load (e.g. pytest-xdist with N
# concurrent RA processes), RA may take >0.5s to apply the toggle before
# GET_STATUS reports PAUSED. Retry the verify a few times instead of
# failing on the first miss.
PAUSE_VERIFY_RETRIES = 5
PAUSE_VERIFY_INTERVAL_S = 0.3

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
        nci_port: int | None = None,
    ) -> "RAHarness":
        """Launch RetroArch headless with the given ROM and core.

        Args:
            rom_path: Path to the ROM file to load.
            core_path: Path to the libretro core (.dll/.so).
            retroarch_exe: Path to the retroarch executable.
            extra_cfg: Additional retroarch.cfg key=value pairs to append to the
                null-driver appendconfig. Used to override settings like
                savestate_directory for tests that need an isolated save dir.
            nci_port: UDP port for the launched RA's Network Command Interface.
                ``None`` uses ``DEFAULT_PORT`` (55355). The chosen port is written
                into the appendconfig as ``network_cmd_port`` and is also the
                port the returned ``NCIClient`` talks to — letting two harnesses
                run in the same pytest session by using distinct ports.
        """
        for p, label in [(retroarch_exe, "retroarch_exe"), (core_path, "core_path"), (rom_path, "rom_path")]:
            if not p.exists():
                raise RAHarnessLaunchError(f"{label} does not exist: {p}")

        port = nci_port if nci_port is not None else DEFAULT_PORT

        # Write a temporary appendconfig to enable null drivers + pin the NCI
        # port. ``--appendconfig`` keys override the user's retroarch.cfg, so
        # specifying ``network_cmd_port`` here lets multiple harnesses run
        # concurrently on distinct ports regardless of what the user's cfg has.
        # ``network_cmd_enable`` is intentionally NOT set here — the user's
        # cfg must already enable NCI, or no harness ever works.
        tmp_cfg_fd, tmp_cfg_path_str = tempfile.mkstemp(suffix=".cfg", prefix="spinlab_ra_null_")
        tmp_cfg_path = Path(tmp_cfg_path_str)
        try:
            with open(tmp_cfg_fd, "w") as f:
                f.write(_NULL_DRIVER_CFG)
                f.write(f'network_cmd_port = "{port}"\n')
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
        logger.info("ra_harness: launching %s on NCI port %d", cmd, port)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        client = NCIClient(port=port)
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
            # Re-send PAUSE_TOGGLE on each iteration if still PLAYING.
            # Under xdist contention the toggle is sometimes lost or RA's
            # NCI thread is starved before processing it; idle waiting
            # alone never recovers. Toggle is safe to re-send only when
            # PLAYING (sending while PAUSED would un-pause).
            after_state: str | None = "PLAYING"
            last_exc: Exception | None = None
            for _ in range(PAUSE_VERIFY_RETRIES):
                if after_state == "PLAYING":
                    try:
                        client.pause_toggle()
                    except Exception:
                        # Best-effort — keep trying. The next get_status
                        # call surfaces a persistent error.
                        pass
                time.sleep(PAUSE_VERIFY_INTERVAL_S)
                try:
                    after = client.get_status()
                except Exception as exc:
                    last_exc = exc
                    after_state = None
                    continue
                after_state = after.state
                if after_state == "PAUSED":
                    break
            else:
                cls._kill(proc)
                tmp_cfg_path.unlink(missing_ok=True)
                if last_exc is not None and after_state is None:
                    raise RAHarnessLaunchError(
                        f"GET_STATUS after pause_toggle kept failing: {last_exc}"
                    ) from last_exc
                raise RAHarnessLaunchError(
                    f"PAUSE_TOGGLE did not pause RA after "
                    f"{PAUSE_VERIFY_RETRIES} retries "
                    f"(last status={after_state!r})"
                )
        else:
            cls._kill(proc)
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(
                f"Unexpected RA status after launch: {status.state!r} — expected PAUSED or PLAYING"
            )

        # Final sanity: confirm FRAMEADVANCE actually advances the core.
        # Read any WRAM byte, advance one frame, re-read — some byte must change.
        # Retry a handful of times: on Windows + patched RA the first one or
        # two FRAMEADVANCE calls after launch occasionally produce no observable
        # WRAM change (likely runahead/save-buffer warm-up). A genuine
        # deep-freeze would still fail every retry.
        advanced = False
        snap_before = client.read_ram(0x0000, WRAM_SANITY_PROBE_BYTES)
        for _ in range(WRAM_SANITY_RETRIES):
            client.frame_advance()
            time.sleep(WRAM_SANITY_RETRY_DELAY_S)
            snap_after = client.read_ram(0x0000, WRAM_SANITY_PROBE_BYTES)
            if snap_before != snap_after:
                advanced = True
                break
            snap_before = snap_after
        if not advanced:
            cls._kill(proc)
            tmp_cfg_path.unlink(missing_ok=True)
            raise RAHarnessLaunchError(
                f"FRAMEADVANCE did not change any WRAM byte after "
                f"{WRAM_SANITY_RETRIES} attempts — core may be in deep-freeze"
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
