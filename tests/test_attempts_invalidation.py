from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource


def _seed(db):
    db.upsert_game("g", "Game", "any%")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,"
        " end_type, end_ordinal, created_at, updated_at)"
        " VALUES ('s1', 'g', 1, 'entrance', 0, 'goal', 0, '2026-01-01', '2026-01-01')"
    )
    db.conn.commit()


def _attempt(sid="sess1"):
    return Attempt(segment_id="s1", session_id=sid, completed=True,
                   time_ms=1000, source=AttemptSource.PRACTICE)


def test_set_attempt_invalidated():
    db = Database(":memory:")
    _seed(db)
    aid = db.log_attempt(_attempt())
    db.set_attempt_invalidated(aid, True)
    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 1
    db.set_attempt_invalidated(aid, False)
    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 0


def test_get_last_practice_attempt():
    db = Database(":memory:")
    _seed(db)
    a1 = db.log_attempt(_attempt(sid="sess1"))
    a2 = db.log_attempt(_attempt(sid="sess1"))
    last = db.get_last_practice_attempt(session_id="sess1")
    assert last is not None
    assert last == a2


def test_get_last_practice_attempt_none_when_empty():
    db = Database(":memory:")
    _seed(db)
    assert db.get_last_practice_attempt(session_id="sess1") is None
