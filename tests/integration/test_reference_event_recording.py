"""Reference recording with deaths produces real per-event attempt rows.

Drives a full reference session through ReferenceController with deaths
in each segment, then asserts the events in `attempts` reflect actual
wall-clock deltas (not synthesized penalty math).

This is the end-to-end gate for the segments-v07 reference-event-level
refactor. Pure Python — no live emulator, no network I/O.
"""
import pytest

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.models import Mode
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
from tests.conftest import FakeEmuBackend

# Override the module-wide emulator mark from tests/integration/conftest.py.
pytestmark = []


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "ref.db"))
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.mark.asyncio
async def test_reference_run_with_deaths_writes_event_rows(db, tmp_path):
    """Reference run with 1 death before cp1 and 2 deaths before goal.

    Per-event time_ms is the wall-clock delta since the previous event in
    the episode (or segment-start for the first event):
      seg1 (entrance→cp1):
        died at t=2000 → time_ms = 2000-1000 = 1000
        survived at cp1 t=4500 → time_ms = 4500-2000 = 2500
      seg2 (cp1→goal):
        died at t=5500 → time_ms = 5500-4500 = 1000
        died at t=7000 → time_ms = 7000-5500 = 1500
        survived at goal t=9000 → time_ms = 9000-7000 = 2000

    Episode_ids must differ between seg1 and seg2 (one fresh episode per
    segment-pass).
    """
    emu = FakeEmuBackend(connected=True)
    ctl = ReferenceController(db, emu)
    await ctl.start_reference(Mode.IDLE, "smw", tmp_path, run_name="Death-Run")
    run_id = ctl.recorder.capture_run_id
    assert run_id is not None

    # --- Segment 1: entrance → cp1, with one death ---
    await ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000,
    ))
    # The controller's handle_death is sync. Pass a DeathEvent with a real
    # monotonic timestamp; the controller forwards it to the recorder which
    # buffers a died event with delta time_ms = 2000-1000 = 1000.
    ctl.handle_death(DeathEvent(timestamp_ms=2000))
    # Spawn updates the recorder's _last_spawn_ms but does NOT advance
    # _last_event_ms — that only moves on died/survived events. So the
    # following survived delta is checkpoint-t=4500 minus death-t=2000 = 2500,
    # not checkpoint-t=4500 minus spawn-t=3000 = 1500.
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000,
    ), game_id="smw")
    await ctl.handle_checkpoint(CheckpointEvent(
        level_num=1, cp_ordinal=1, timestamp_ms=4500,
    ), game_id="smw")

    # --- Segment 2: cp1 → goal, with two deaths ---
    ctl.handle_death(DeathEvent(timestamp_ms=5500))   # delta = 5500-4500 = 1000
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=6000,
    ), game_id="smw")
    ctl.handle_death(DeathEvent(timestamp_ms=7000))   # delta = 7000-5500 = 1500
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=7500,
    ), game_id="smw")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="normal", timestamp_ms=9000,
    ), game_id="smw")

    # All event rows for this run, in insertion order.
    rows = db.conn.execute(
        "SELECT segment_id, episode_id, outcome, time_ms, source "
        "FROM attempts WHERE capture_run_id = ? ORDER BY id", (run_id,),
    ).fetchall()

    # Expected: 5 events total (1 died + 1 survived for seg1;
    #                            2 died + 1 survived for seg2).
    assert len(rows) == 5, f"expected 5 events, got {len(rows)}: {[dict(r) for r in rows]}"
    seg_ids = {r["segment_id"] for r in rows}
    assert len(seg_ids) == 2, (
        f"expected 2 distinct segments, got {len(seg_ids)}: "
        f"{[(sid, [r['outcome'] for r in rows if r['segment_id'] == sid]) for sid in seg_ids]}"
    )

    # Group by segment_id and check shape per segment.
    by_seg: dict[str, list[dict]] = {}
    for r in rows:
        by_seg.setdefault(r["segment_id"], []).append(dict(r))

    # Each segment's events share one episode_id; episode_ids differ across segments.
    ep_ids = []
    for seg_id, events in by_seg.items():
        eids = {e["episode_id"] for e in events}
        assert len(eids) == 1, f"segment {seg_id} events span multiple episodes: {eids}"
        ep_ids.append(next(iter(eids)))
    assert len(set(ep_ids)) == 2, "episode_ids must differ across segments"

    # Verify per-segment outcomes and time deltas. Identify segments by the
    # number of events (seg1 has 2, seg2 has 3) — the guard below makes that
    # disambiguation explicit so a future refactor making the counts equal
    # fails loudly instead of silently picking the wrong segment.
    counts = sorted(len(es) for es in by_seg.values())
    assert counts == [2, 3], (
        f"expected segment event counts [2, 3], got {counts} — "
        f"segment identification by count is ambiguous if both are equal"
    )
    seg1_events = next(es for es in by_seg.values() if len(es) == 2)
    seg2_events = next(es for es in by_seg.values() if len(es) == 3)

    assert [e["outcome"] for e in seg1_events] == ["died", "survived"]
    assert [e["time_ms"] for e in seg1_events] == [1000, 2500]
    assert all(e["source"] == "reference" for e in seg1_events)

    assert [e["outcome"] for e in seg2_events] == ["died", "died", "survived"]
    assert [e["time_ms"] for e in seg2_events] == [1000, 1500, 2000]
    assert all(e["source"] == "reference" for e in seg2_events)
