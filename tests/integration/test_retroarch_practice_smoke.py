"""Live-RA smoke test for Phase F-live orchestrator wiring.

Skipped when RetroArch is not running on default NCI port. Andrew runs
`pytest -m emulator -v` after launching RA. NOT the migration's smoke gate
(that's Andrew exercising the dashboard manually) — this is a startup sanity
check that the orchestrator can establish NCI and emit a rom_info event.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.retroarch.exceptions import NCIError, NCITimeout
from spinlab.retroarch.nci import NCIClient


def _ra_alive() -> bool:
    """Probe NCI on default port; return True if RA answers VERSION."""
    try:
        client = NCIClient()
        _ = client.version()
        client.close()
        return True
    except (NCIError, NCITimeout):
        return False


@pytest.mark.emulator
def test_orchestrator_connects_to_live_retroarch(tmp_path):
    """Smoke: orchestrator can issue VERSION and emit rom_info via NCI."""
    if not _ra_alive():
        pytest.skip("RetroArch not running on default NCI port (55355)")

    # Build minimal config pointed at temp dirs.
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            backend="retroarch",
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="LiveRATest",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    (tmp_path / "ra").mkdir()
    (tmp_path / "sl").mkdir()

    from spinlab.retroarch.orchestrator import build_orchestrator
    orch = build_orchestrator(cfg)

    import asyncio

    async def _run() -> tuple[bool, dict | None]:
        ok = await orch.connect()
        ev = None
        if ok:
            ev = await orch.recv_event(timeout=0.5)
        await orch.disconnect()
        return ok, ev

    ok, first_event = asyncio.run(_run())

    assert ok is True, "orchestrator could not connect to live RetroArch"
    assert first_event is not None, "no rom_info event from connect()"
    assert first_event.get("event") == "rom_info"
    # Filename comes from RA's GET_STATUS — non-empty if RA has a ROM loaded.
    # If filename is empty it just means no ROM is loaded; not a smoke fail.
    assert "filename" in first_event
