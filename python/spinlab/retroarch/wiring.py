"""build_orchestrator — config-driven construction of a RetroArchOrchestrator.

Separated from orchestrator.py so that module stays purely about
implementing the EmuBackend protocol. This module owns AppConfig parsing,
path resolution (movie dir, RA log dir), and dependency-injection wiring.
"""
from __future__ import annotations

import logging
from pathlib import Path

from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.movies import MovieController
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.retroarch.raclient import RAClient
from spinlab.state_paths import StatePathResolver
from spinlab.timing import PracticeTiming, SpeedRunTiming

logger = logging.getLogger(__name__)


def build_orchestrator(config) -> RetroArchOrchestrator:
    """Construct a fully wired RetroArchOrchestrator from AppConfig.

    Raises ValueError if config.emulator is missing required RA fields.
    """
    emu = config.emulator
    missing = [
        name
        for name, val in (
            ("savestate_dir", emu.savestate_dir),
            ("spinlab_state_dir", emu.spinlab_state_dir),
        )
        if val is None
    ]
    if missing:
        raise ValueError(
            f"build_orchestrator: emulator.{', emulator.'.join(missing)} required for retroarch backend"
        )
    # Type-narrow for the rest of the function — checked above.
    savestate_dir: Path = emu.savestate_dir
    spinlab_state_dir: Path = emu.spinlab_state_dir

    from spinlab.retroarch.poller import DEFAULT_PERIOD_SEC, Poller, PollerDeps
    from spinlab.retroarch.snapshot import read_snapshot

    # Resolve where RA writes movie files. Priority:
    #   1. emu.ra_movie_dir (explicit override)
    #   2. emu.savestate_dir / emu.ra_core_subdir (derived from existing config)
    #   3. None — disables recorder/player
    movie_dir: Path | None
    if emu.ra_movie_dir is not None:
        movie_dir = emu.ra_movie_dir
    elif emu.ra_core_subdir:
        # Don't double-append: users sometimes set savestate_dir to the
        # per-core subdir directly (e.g. C:\RetroArch-Win64\states\Snes9x).
        if savestate_dir.name == emu.ra_core_subdir:
            movie_dir = savestate_dir
        else:
            movie_dir = savestate_dir / emu.ra_core_subdir
        logger.info("build_orchestrator: movie_dir derived as %s", movie_dir)
    else:
        logger.info(
            "build_orchestrator: movie recorder/player disabled — set emu.ra_movie_dir "
            "or emu.savestate_dir + emu.ra_core_subdir to enable",
        )
        movie_dir = None

    # RA's logs dir, derived from retroarch_path. Enables replay-slot
    # resolution; falls back to slot 0 if unavailable.
    ra_log_dir: Path | None = None
    if emu.retroarch_path is not None:
        candidate = emu.retroarch_path.parent / "logs"
        if candidate.exists():
            ra_log_dir = candidate
        else:
            logger.info(
                "build_orchestrator: RA logs dir not found at %s — replay "
                "slot lookup will fall back to slot 0. Enable log_to_file "
                'in retroarch.cfg to fix.', candidate,
            )

    raclient = RAClient(
        host=config.network.host,
        port=config.network.nci_port,
        ra_savestate_dir=savestate_dir,
        ra_log_dir=ra_log_dir,
        ra_movie_dir=movie_dir,
    )

    conditions = ConditionRegistry()
    practice_timing = PracticeTiming()
    speed_run_timing = SpeedRunTiming()
    state_paths = StatePathResolver(spinlab_state_dir)

    deps = PollerDeps(
        client=raclient.nci,
        read_snapshot=read_snapshot,
        on_event=lambda ev: None,  # rebound below
        state_path_for=state_paths.resolve_event,
        conditions_registry=conditions,
        state_version=lambda: raclient.state_version,
    )
    poller = Poller(deps, period_sec=DEFAULT_PERIOD_SEC)

    movies = MovieController(
        movie_io=raclient.movie_io,
        raclient=raclient,
        enable=movie_dir is not None,
        on_event=lambda ev: None,  # rebound by orch.__init__
        on_replay_started=poller.mark_replay_entrance,
    )
    orch = RetroArchOrchestrator(
        raclient=raclient,
        poller=poller,
        conditions=conditions,
        practice_timing=practice_timing,
        speed_run_timing=speed_run_timing,
        state_paths=state_paths,
        movies=movies,
    )
    deps.on_event = orch.on_poller_event
    return orch
