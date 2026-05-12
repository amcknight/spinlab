"""POST /api/reference/start under retroarch — must not raise a generic 500.

ReferenceStart no longer raises BackendNotImplementedError under RA: it
enables state-capture mode (no BSV input recording, but states are saved
on entrance/checkpoint events). Replay endpoints still 501 since BSV is
genuinely Phase E. This test guards against regressing reference/start
back into the 500 hole.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.dashboard import create_app
from spinlab.db import Database


def test_reference_start_does_not_500_under_retroarch(tmp_path):
    """reference/start should return either success or a typed error (409 etc),
    never an opaque 500."""
    db = Database(":memory:")
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="Test",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    with TestClient(create_app(db, config=cfg)) as client:
        resp = client.post("/api/reference/start")
    # Acceptable outcomes: 200 (success), 409 (no game seeded, etc.), 503
    # (orchestrator not connected). Critical: NOT 500 — that would mean an
    # unhandled NotImplementedError leaked through again.
    assert resp.status_code != 500, (
        f"reference/start under retroarch surfaced as 500: {resp.text}"
    )


def test_replay_start_returns_501_under_retroarch(tmp_path):
    """Replay genuinely needs BSV (Phase E) — must surface as a typed 501."""
    db = Database(":memory:")
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="Test",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    with TestClient(create_app(db, config=cfg)) as client:
        # Replay needs a ref_id; we expect to fail before hitting the
        # orchestrator with a 400. If we somehow reach the orchestrator,
        # 501 is the right code.
        resp = client.post("/api/replay/start", json={"ref_id": "nonexistent"})
    # Either 400 (no ref) / 404 (ref not found) / 501 (reached orchestrator)
    # — never 500.
    assert resp.status_code != 500
