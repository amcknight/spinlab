from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource


def test_log_attempt_persists_observed_conditions():
    db = Database(":memory:")
    db.upsert_game("g", "Game", "any%")
    # Minimal segment to satisfy FK
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, created_at, updated_at) "
        "VALUES ('s1', 'g', 1, 'entrance', 0, 'goal', 0, '2026-01-01', '2026-01-01')"
    )
    db.conn.commit()
    db.log_attempt(Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE,
        observed_start_conditions='{"powerup": "big"}',
        observed_end_conditions='{"powerup": "small"}',
    ))
    row = db.conn.execute(
        "SELECT observed_start_conditions, observed_end_conditions, invalidated "
        "FROM attempts WHERE segment_id = 's1'").fetchone()
    assert row[0] == '{"powerup": "big"}'
    assert row[1] == '{"powerup": "small"}'
    assert row[2] == 0
