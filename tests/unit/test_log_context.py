"""Tests that verify each CF-3 site logs the right context on failure."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# M10 — speed_run teardown
# ---------------------------------------------------------------------------


class _DisconnectedEmu:
    """Minimal EmuBackend stub: raises ConnectionError on send_command.

    is_connected starts True so run_loop's outer ``while`` enters once;
    the inner run_one path returns False (empty levels), and the finally
    block then attempts ``send_command(SpeedRunStopCmd())`` which raises.
    """

    def __init__(self) -> None:
        self.is_connected = True

    async def send_command(self, cmd: object) -> None:
        raise ConnectionError("backend gone")


def test_speed_run_teardown_logs_when_backend_disconnects(caplog):
    """speed_run.run_loop's finally must log, not silently pass, on backend-gone."""
    from spinlab.speed_run import SpeedRunSession

    emu = _DisconnectedEmu()
    # SpeedRunSession.__init__ calls db.get_all_segments_with_model (we want [] so
    # levels stays empty and run_one returns False immediately) and
    # db.create_session.  stop() calls db.end_session.  All other attrs are unused.
    db = MagicMock()
    db.get_all_segments_with_model.return_value = []

    session = SpeedRunSession(emu=emu, db=db, game_id="g")  # type: ignore[arg-type]

    caplog.set_level(logging.INFO, logger="spinlab.speed_run")
    asyncio.run(session.run_loop())

    messages = [r.getMessage() for r in caplog.records if r.name == "spinlab.speed_run"]
    assert any("speed_run teardown" in m for m in messages), (
        f"expected a 'speed_run teardown' log line; got: {messages!r}"
    )
