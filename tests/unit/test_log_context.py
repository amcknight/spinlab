"""Tests that verify each CF-3 site logs the right context on failure."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

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


# ---------------------------------------------------------------------------
# M11 — orchestrator tick error includes timing state
# ---------------------------------------------------------------------------

def test_tick_loop_logs_state_on_error(caplog):
    """When a tick raises, the log must include practice_armed / speed_run_armed."""
    from spinlab.retroarch.orchestrator import RetroArchOrchestrator

    class _Boom:
        is_armed = True
        def tick(self) -> None:
            raise RuntimeError("kaboom")

    class _Quiet:
        is_armed = False
        def tick(self) -> None:
            pass

    orch = RetroArchOrchestrator.__new__(RetroArchOrchestrator)  # bypass __init__
    orch._practice_timing = _Boom()
    orch._speed_run_timing = _Quiet()
    orch._running = True

    import spinlab.retroarch.orchestrator as orch_mod

    async def _stop_after(_delay: float) -> None:
        orch._running = False

    real_sleep = orch_mod.asyncio.sleep
    orch_mod.asyncio.sleep = _stop_after  # type: ignore[assignment]
    try:
        caplog.set_level(logging.ERROR, logger="spinlab.retroarch.orchestrator")
        asyncio.run(orch._tick_loop())
    finally:
        orch_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]

    records = [
        r for r in caplog.records
        if r.name == "spinlab.retroarch.orchestrator" and "tick error" in r.getMessage()
    ]
    assert records, "expected a tick error log line"
    msg = records[0].getMessage()
    assert "practice_armed=True" in msg, msg
    assert "speed_run_armed=False" in msg, msg


# ---------------------------------------------------------------------------
# M12 — NCI _drain_socket logs late datagram count
# ---------------------------------------------------------------------------

def test_drain_socket_logs_drained_datagram_count(caplog):
    """When a stale datagram is drained, log warn with count=1."""
    import socket

    from spinlab.retroarch.nci import NCIClient

    fake_ra = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake_ra.bind(("127.0.0.1", 0))
    fake_ra_port = fake_ra.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_sock.bind(("127.0.0.1", 0))
    client_port = client_sock.getsockname()[1]
    client_sock.settimeout(0.5)

    fake_ra.sendto(b"late reply", ("127.0.0.1", client_port))

    client = NCIClient(host="127.0.0.1", port=fake_ra_port, timeout=0.5)

    caplog.set_level(logging.WARNING, logger="spinlab.retroarch.nci")
    client._drain_socket(client_sock)

    client_sock.close()
    fake_ra.close()

    records = [r for r in caplog.records if "drained late" in r.getMessage().lower()]
    assert records, "expected drained-late-datagram log line"
    assert "count=1" in records[0].getMessage(), records[0].getMessage()


def test_drain_socket_silent_when_nothing_drained(caplog):
    """No log emitted when the buffer was already empty."""
    import socket

    from spinlab.retroarch.nci import NCIClient

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.5)

    client = NCIClient(host="127.0.0.1", port=1, timeout=0.5)

    caplog.set_level(logging.WARNING, logger="spinlab.retroarch.nci")
    client._drain_socket(sock)
    sock.close()

    assert not [r for r in caplog.records if "drained late" in r.getMessage().lower()], (
        "drain should be silent when nothing was drained"
    )


# ---------------------------------------------------------------------------
# M14 — SSE broadcaster logs dropped subscriber
# ---------------------------------------------------------------------------

def test_sse_broadcaster_logs_dropped_subscriber(caplog):
    """When recovery fails and a subscriber is unsubscribed, log warn."""
    from spinlab.sse import SSEBroadcaster

    broadcaster = SSEBroadcaster()
    q = broadcaster.subscribe(maxsize=1)
    import asyncio as _asyncio

    def _always_full(_item: object) -> None:
        raise _asyncio.QueueFull()

    # Pre-fill so the initial put_nowait raises; then make recovery put also fail.
    q.put_nowait("first")
    q.put_nowait = _always_full  # type: ignore[assignment]

    caplog.set_level(logging.WARNING, logger="spinlab.sse")
    asyncio.run(broadcaster.broadcast({"hello": "world"}))

    records = [r for r in caplog.records if "subscriber dropped" in r.getMessage().lower()]
    assert records, "expected SSE subscriber-dropped log line"
    assert "subscribers_left=0" in records[0].getMessage(), records[0].getMessage()


# ---------------------------------------------------------------------------
# M15 — estimators.load_mature_states logs corrupt state_json
# ---------------------------------------------------------------------------

def test_load_mature_states_logs_corrupt_json(caplog):
    """Corrupt state_json row must produce a warn with seg/estimator/game context."""
    from dataclasses import dataclass

    from spinlab.estimators import EstimatorState, load_mature_states

    @dataclass
    class _DummyState(EstimatorState):
        def to_dict(self) -> dict:
            return {"n_completed": self.n_completed}

        @classmethod
        def from_dict(cls, d: dict) -> "_DummyState":
            return cls(n_completed=d["n_completed"])

    class _StubDB:
        def load_all_model_states(self, game_id: str) -> list[dict]:
            return [
                {"segment_id": "seg-corrupt", "estimator": "dummy", "state_json": "{not json"},
                {"segment_id": "seg-ok",      "estimator": "dummy", "state_json": '{"n_completed": 10}'},
            ]

    caplog.set_level(logging.WARNING, logger="spinlab.estimators")
    results = load_mature_states(
        db=_StubDB(),  # type: ignore[arg-type]
        game_id="game-x",
        estimator_name="dummy",
        state_cls=_DummyState,
        maturity_threshold=5,
    )

    # The good row should survive
    assert len(results) == 1
    assert results[0].n_completed == 10

    # The bad row should have produced a log line with context
    records = [r for r in caplog.records if "corrupt estimator state" in r.getMessage().lower()]
    assert records, "expected corrupt-state-json log line"
    msg = records[0].getMessage()
    assert "segment_id='seg-corrupt'" in msg, msg
    assert "estimator='dummy'" in msg, msg
    assert "game_id='game-x'" in msg, msg


# ---------------------------------------------------------------------------
# M9 — condition_registry.from_yaml error includes path
# ---------------------------------------------------------------------------

def test_from_yaml_error_includes_path(tmp_path):
    """A malformed conditions.yaml must raise with the yaml path in the message."""
    from spinlab.condition_registry import ConditionRegistry

    bad_yaml = tmp_path / "conditions.yaml"
    bad_yaml.write_text(
        # Missing 'name' key — will trigger KeyError("name") in the parse loop.
        "conditions:\n"
        "  - scope: game\n"
        "    address: 0x1000\n"
        "    size: 1\n"
        "    type: u8\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        ConditionRegistry.from_yaml(bad_yaml)

    msg = str(exc_info.value)
    assert str(bad_yaml) in msg, f"path not in error message: {msg!r}"


def test_from_yaml_typeerror_also_wrapped(tmp_path):
    """A None-address triggers TypeError(int(None)); it must also be wrapped with path."""
    from spinlab.condition_registry import ConditionRegistry

    bad_yaml = tmp_path / "conditions.yaml"
    bad_yaml.write_text(
        # `address:` with no value parses as None; int(None) raises TypeError.
        "conditions:\n"
        "  - name: x\n"
        "    scope: game\n"
        "    address:\n"
        "    size: 1\n"
        "    type: u8\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        ConditionRegistry.from_yaml(bad_yaml)

    assert str(bad_yaml) in str(exc_info.value)
    # Make sure the underlying cause is preserved as TypeError, not silently swallowed.
    assert isinstance(exc_info.value.__cause__, TypeError)
