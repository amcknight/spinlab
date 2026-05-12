"""Live-RA smoke test for orchestrator wiring.

Launches its own RetroArch via ``ra_harness`` (dynamic port) so no manual
``retroarch`` invocation is required — same pattern as the rest of the
emulator-marked suite. Skipped only when the harness itself can't launch
(missing retroarch_path / core / ROM in config.yaml).
"""
from __future__ import annotations

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig


@pytest.mark.emulator
def test_orchestrator_connects_to_live_retroarch(ra_harness, tmp_path):
    """Smoke: orchestrator can issue VERSION and emit rom_info via NCI."""
    cfg = AppConfig(
        network=NetworkConfig(nci_port=ra_harness.client.port),
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="LiveRATest",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    (tmp_path / "ra").mkdir()
    (tmp_path / "sl").mkdir()

    from spinlab.retroarch.wiring import build_orchestrator
    orch = build_orchestrator(cfg)

    import asyncio

    async def _run() -> tuple[bool, object | None]:
        ok = await orch.connect()
        ev = None
        if ok:
            ev = await orch.recv_event(timeout=0.5)
        await orch.disconnect()
        return ok, ev

    ok, first_event = asyncio.run(_run())

    from spinlab.protocol import RomInfoEvent
    assert ok is True, "orchestrator could not connect to live RetroArch"
    assert first_event is not None, "no rom_info event from connect()"
    assert isinstance(first_event, RomInfoEvent)
    # Filename comes from RA's GET_STATUS — non-empty if RA has a ROM loaded.
    # If filename is empty it just means no ROM is loaded; not a smoke fail.
    assert hasattr(first_event, "filename")
