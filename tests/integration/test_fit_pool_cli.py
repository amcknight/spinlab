"""End-to-end: spinlab fit-pool over a tmp DB with seeded segments."""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

pytest.importorskip("jax")
pytest.importorskip("numpyro")


def _seed_db(db_path, data_dir):
    from spinlab.db import Database
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt

    db = Database(db_path)
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) VALUES "
        "('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    # Seed one session — log_event_attempt requires a session_id FK target.
    db.conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) "
        "VALUES ('sess-1', 'g1', '2026-05-19T00:00:00Z')"
    )
    now = datetime.now(UTC)
    # Five segments, each with ≥5 attempts so they meet the pool floor.
    for sid in [f"s{i}" for i in range(5)]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, "
            "start_type, end_type, created_at, updated_at) "
            "VALUES (?, 'g1', 1, 'entrance', 'goal', "
            "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
            (sid,),
        )
        ep = f"ep-{sid}"
        for _ in range(10):
            db.log_event_attempt(EventAttempt(
                segment_id=sid, episode_id=ep,
                outcome=AttemptOutcome.SURVIVED, time_ms=20000,
                source=AttemptSource.PRACTICE,
                session_id="sess-1", capture_run_id=None,
                chosen_allocator=None, invalidated=False,
                created_at=now,
            ))
    db.conn.commit()


def test_fit_pool_writes_pool_kind_rows(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "spinlab.db"
    _seed_db(db_path, data_dir)
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(
        f"data:\n  dir: {data_dir.as_posix()}\n"
        f"game:\n  category: any%\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit-pool",
         "--config", str(cfg), "--game", "g1"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    from spinlab.db import Database
    db = Database(db_path)
    rows = db.conn.execute(
        "SELECT segment_id, kind FROM segment_fits WHERE kind='pool_fit'"
    ).fetchall()
    # One pool-kind row per segment in the pool.
    assert len(rows) == 5
