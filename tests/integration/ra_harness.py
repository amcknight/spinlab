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
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from tests.integration.ra_poke_engine import RAPokeEngine

from spinlab.retroarch.exceptions import NCITimeout
from spinlab.retroarch.nci import DEFAULT_PORT, NCIClient

logger = logging.getLogger(__name__)

# RA needs a moment after Popen before NCI starts replying.
NCI_PING_RETRIES = 10
NCI_PING_INTERVAL_S = 0.5

# Teardown timing.
QUIT_GRACE_S = 2.0

# PAUSE_TOGGLE → status check race. Under load (e.g. multiple concurrent RA
# processes — currently up to 3 in the emulator suite: vanilla_smw,
# love_yourself, love_yourself_no_reset), RA may take >1s to apply the
# toggle before GET_STATUS reports PAUSED. Retry the verify generously
# instead of failing on the first miss. 10 × 0.3s = 3s total budget.
PAUSE_VERIFY_RETRIES = 10
PAUSE_VERIFY_INTERVAL_S = 0.3

# When the harness is given a fresh_state_path, the state file is copied into
# the harness's isolated savestate_directory at this slot. RAPokeEngine then
# LOAD_STATE_SLOTs this slot before each scenario for hermetic per-test boot.
FRESH_BOOT_STATE_SLOT = 9998

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
    log_path: Path | None = field(default=None, repr=False)
    _log_handle: object = field(default=None, repr=False)
    _tmp_dir: Path | None = field(default=None, repr=False)
    fresh_boot_slot: int | None = field(default=None, repr=False)
    engine: RAPokeEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = RAPokeEngine(self.client, fresh_boot_slot=self.fresh_boot_slot)

    @classmethod
    def launch(
        cls,
        rom_path: Path,
        core_path: Path,
        retroarch_exe: Path,
        extra_cfg: str = "",
        nci_port: int | None = None,
        fresh_state_path: Path | None = None,
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
            fresh_state_path: Optional path to a pre-recorded "fresh boot"
                savestate. When set, the harness isolates ``savestate_directory``
                to a per-launch tmp dir, copies the file in at slot
                ``FRESH_BOOT_STATE_SLOT``, and configures the bound
                ``RAPokeEngine`` to ``LOAD_STATE_SLOT`` it before each scenario.
                This is the per-scenario boot mechanism — replaces the old
                FRAMEADVANCE warm-up probe that intermittently rejected
                valid ROMs.
        """
        for p, label in [(retroarch_exe, "retroarch_exe"), (core_path, "core_path"), (rom_path, "rom_path")]:
            if not p.exists():
                raise RAHarnessLaunchError(f"{label} does not exist: {p}")

        port = nci_port if nci_port is not None else DEFAULT_PORT

        # Per-launch isolation directory. Holds the appendconfig, plus an
        # empty SRAM subdir that we point RA at via ``sram_directory``.
        # Without SRAM isolation, RA's libretro layer auto-loads any existing
        # ``<user_saves_dir>/<core>/<rom>.srm`` on boot, which can deep-freeze
        # the core when the SRAM is stale or was written by a different
        # ROM/build (root cause of all-day FRAMEADVANCE-sanity failures on
        # Toothpaste prior to this isolation).
        #
        # ``savestate_directory`` is *not* overridden here — the dashboard's
        # replay flow expects to write/read .replay files in the user's
        # configured savestate dir, and isolating it desyncs RA from the
        # dashboard. Savestates aren't auto-loaded (``savestate_auto_load``
        # is "false" in the user cfg), so they don't trigger the SRAM-style
        # deep-freeze.
        tmp_dir = Path(tempfile.mkdtemp(prefix="spinlab_ra_"))
        sram_dir = tmp_dir / "saves"
        sram_dir.mkdir()
        tmp_cfg_path = tmp_dir / "null.cfg"
        # RA cfg paths use forward slashes even on Windows.
        sram_cfg = sram_dir.as_posix()

        # When a fresh-boot savestate is supplied, isolate ``savestate_directory``
        # to a per-launch tmp dir and stage the state file into it at
        # FRESH_BOOT_STATE_SLOT, named by snes9x's <rom_basename>.state{N}
        # convention. The poke harness's tests do not rely on the user's
        # savestate dir, so this isolation is safe; the replay fixture (which
        # DOES rely on the user's savestate dir) launches the harness without
        # ``fresh_state_path`` and keeps the user dir untouched.
        savestate_dir: Path | None = None
        if fresh_state_path is not None:
            if not fresh_state_path.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise RAHarnessLaunchError(
                    f"fresh_state_path does not exist: {fresh_state_path}"
                )
            savestate_dir = tmp_dir / "states"
            snes9x_subdir = savestate_dir / "Snes9x"
            snes9x_subdir.mkdir(parents=True)
            staged_state = snes9x_subdir / f"{rom_path.stem}.state{FRESH_BOOT_STATE_SLOT}"
            shutil.copyfile(fresh_state_path, staged_state)

        # ``--appendconfig`` keys override the user's retroarch.cfg, so
        # specifying ``network_cmd_port`` here lets multiple harnesses run
        # concurrently on distinct ports regardless of what the user's cfg has.
        # ``network_cmd_enable`` is intentionally NOT set here — the user's
        # cfg must already enable NCI, or no harness ever works.
        try:
            with open(tmp_cfg_path, "w") as f:
                f.write(_NULL_DRIVER_CFG)
                f.write(f'network_cmd_port = "{port}"\n')
                f.write(f'sram_directory = "{sram_cfg}"\n')
                if savestate_dir is not None:
                    f.write(f'savestate_directory = "{savestate_dir.as_posix()}"\n')
                if extra_cfg:
                    f.write(extra_cfg)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        cmd = [
            str(retroarch_exe),
            f"--appendconfig={tmp_cfg_path}",
            "-L", str(core_path),
            str(rom_path),
        ]
        logger.info("ra_harness: launching %s on NCI port %d", cmd, port)
        log_path = tmp_dir / "retroarch.log"
        log_handle = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=log_handle,
            )
        except Exception:
            log_handle.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        client = NCIClient(port=port)
        # Ping until NCI replies or we exhaust retries.
        for attempt in range(NCI_PING_RETRIES):
            try:
                client.version()
                break
            except NCITimeout:
                time.sleep(NCI_PING_INTERVAL_S)
        else:
            cls._cleanup_launch(proc, log_handle, tmp_dir)
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
            cls._cleanup_launch(proc, log_handle, tmp_dir)
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
                cls._cleanup_launch(proc, log_handle, tmp_dir)
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
            cls._cleanup_launch(proc, log_handle, tmp_dir)
            raise RAHarnessLaunchError(
                f"Unexpected RA status after launch: {status.state!r} — expected PAUSED or PLAYING"
            )

        # No FRAMEADVANCE warm-up probe here. The probe used to read 32 bytes
        # of low WRAM, FRAMEADVANCE, re-read, and reject the launch if no byte
        # changed — but it intermittently rejected valid ROMs (Toothpaste,
        # vanilla SMW, even Love Yourself under load). Replaced by the
        # savestate-based per-scenario reset in RAPokeEngine: a loaded
        # savestate is by construction a live frame, so the probe is moot
        # for poke tests; for non-poke tests (replay) downstream failures
        # surface a deep-frozen core just as well.
        slot = FRESH_BOOT_STATE_SLOT if fresh_state_path is not None else None
        return cls(
            proc=proc,
            client=client,
            log_path=log_path,
            _log_handle=log_handle,
            _tmp_dir=tmp_dir,
            fresh_boot_slot=slot,
        )

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
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

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

    @staticmethod
    def _cleanup_launch(proc: subprocess.Popen, log_handle, tmp_dir: Path) -> None:
        """Tear down a half-launched harness on a launch-failure path."""
        RAHarness._kill(proc)
        try:
            log_handle.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
