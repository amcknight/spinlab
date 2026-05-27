"""SegmentRecorder tags the first event of each episode as hot or cold
depending on whether the episode was armed by a checkpoint (hot — player
carried state from prior segment) or an entrance (cold — level start)."""
from __future__ import annotations

import pytest

from spinlab.db import Database
from spinlab.capture.recorder import SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent, LevelExitEvent


@pytest.fixture
def recorder() -> tuple[SegmentRecorder, Database]:
    db = Database(":memory:")
    db.upsert_game("g1", "TestGame", "any%")
    db.create_capture_run("run1", "g1", "test run")
    db.create_capture_session("sess1", "run1", 1)
    rec = SegmentRecorder(db, ConditionRegistry({}))
    rec.capture_run_id = "run1"
    rec.current_capture_session_id = "sess1"
    return rec, db


def test_first_event_after_entrance_is_cold(recorder):
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=5000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "survived"
    assert rows[0]["is_hot"] == 0


def test_first_event_after_checkpoint_is_hot(recorder):
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=2000,
    ), game_id="g1")
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=5000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot, episode_id FROM attempts ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["is_hot"] == 0
    assert rows[1]["is_hot"] == 1


def test_post_death_events_are_cold(recorder):
    """Within one episode, the first event is hot/cold by arm-source, but
    every post-death event in that same episode is cold (respawn)."""
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=1000,
    ), game_id="g1")
    rec.handle_death(timestamp_ms=2000)
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=4000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts "
        "WHERE episode_id = (SELECT episode_id FROM attempts ORDER BY id DESC LIMIT 1) "
        "ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["outcome"] == "died"
    assert rows[0]["is_hot"] == 1, "first event of cp-armed episode is hot even if it died"
    assert rows[1]["outcome"] == "survived"
    assert rows[1]["is_hot"] == 0, "post-death respawn is cold"


def test_abort_then_entrance_produces_cold(recorder):
    """A cp-armed episode that gets aborted, followed by a fresh entrance,
    must produce a cold first event. Pins behavior that depends on the
    abort path resetting first-of-episode flags."""
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=1000,
    ), game_id="g1")
    # Abort before any deaths in the cp-armed episode.
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="abort", timestamp_ms=2000,
    ), game_id="g1")
    # Fresh entrance and goal — first (and only) event must be cold.
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=3000,
    ))
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=5000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts ORDER BY id"
    ).fetchall()
    # First episode (entrance→cp) wrote 1 survived event (cold).
    # cp-armed episode aborted → no rows.
    # Second entrance→goal wrote 1 survived event (cold).
    assert rows[-1]["outcome"] == "survived"
    assert rows[-1]["is_hot"] == 0, "post-abort entrance produces cold event"


def test_two_consecutive_checkpoints_both_hot(recorder):
    """A level with two checkpoints between entrance and goal should produce
    hot first events for both cp-armed episodes."""
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=1000,
    ), game_id="g1")
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=2, level_num=1, conditions={}, state_path=None, timestamp_ms=2000,
    ), game_id="g1")
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=3000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts ORDER BY id"
    ).fetchall()
    # 3 episodes: entrance→cp1 (cold), cp1→cp2 (hot), cp2→goal (hot).
    assert len(rows) == 3
    assert rows[0]["is_hot"] == 0, "entrance-armed episode is cold"
    assert rows[1]["is_hot"] == 1, "first cp-armed episode is hot"
    assert rows[2]["is_hot"] == 1, "second cp-armed episode is hot"
