"""Round-trip tests for the segment_fits table + Database helpers."""
from __future__ import annotations

import pytest

from spinlab.db import Database


@pytest.fixture()
def db(tmp_path):
    db = Database(tmp_path / "test.db")
    # Seed a game + segment so foreign keys hold.
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()
    return db


def _make_payload(segment_id="s1", n_attempts=42, fittable=True):
    return {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": segment_id,
        "n_attempts": n_attempts,
        "model": "haz1",
        "wall_time_s": 0.015,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False,
            "fittable": fittable,
        },
        "result": {"map": {"log_theta": [0.0] * 10, "natural": {}},
                   "bands": {}, "derived": {}, "ppc": {}},
        "caveats": [] if fittable else ["unconverged"],
    }


def test_save_then_load_latest_returns_same_payload(db):
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=11))
    latest = db.load_latest_segment_fit("s1", "segment_fit")
    assert latest is not None
    assert latest["n_attempts"] == 11  # Newer write wins.


def test_load_latest_returns_none_for_missing_segment(db):
    assert db.load_latest_segment_fit("nope", "segment_fit") is None


def test_pool_kind_is_keyed_separately_from_segment_fit(db):
    seg_payload = _make_payload(n_attempts=10)
    pool_payload = {**_make_payload(n_attempts=999), "kind": "pool_fit"}
    db.save_segment_fit("s1", "segment_fit", seg_payload)
    db.save_segment_fit("s1", "pool_fit", pool_payload)
    assert db.load_latest_segment_fit("s1", "segment_fit")["n_attempts"] == 10
    assert db.load_latest_segment_fit("s1", "pool_fit")["n_attempts"] == 999


def test_iter_recent_fits_orders_newest_first(db):
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=1))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=2))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=3))
    rows = list(db.iter_recent_segment_fits("s1", limit=2))
    assert [r["n_attempts"] for r in rows] == [3, 2]


def test_save_fit_records_extracted_status_fields(db):
    """The mixin pulls status flags into columns so the inspector can
    filter without parsing JSON on every row."""
    db.save_segment_fit("s1", "segment_fit", _make_payload(fittable=False))
    row = db.conn.execute(
        "SELECT fittable, ppc_tension, band_source FROM segment_fits "
        "WHERE segment_id = 's1'"
    ).fetchone()
    assert row["fittable"] == 0
    assert row["ppc_tension"] == 0
    assert row["band_source"] == "laplace"


def test_iter_segment_fit_summaries_returns_one_row_per_segment(db):
    """Multiple fits on the same segment → only the latest is in the summary."""
    # Seed a second segment so we can confirm the helper returns both.
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s2', 'g1', 2, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()

    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=11))
    db.save_segment_fit("s2", "segment_fit", _make_payload(n_attempts=20))

    rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    by_id = {r["segment_id"]: r for r in rows}
    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"]["n_attempts"] == 11  # Latest, not 10.
    assert by_id["s2"]["n_attempts"] == 20
    # Status fields are projected to columns; the helper exposes them.
    assert by_id["s1"]["fittable"] == 1
    assert by_id["s1"]["band_source"] == "laplace"
    # Each row carries the latest fit's payload so the caller can dig into
    # `derived.M_clear` etc. without a second query.
    assert by_id["s1"]["payload"]["n_attempts"] == 11


def test_iter_segment_fit_summaries_skips_segments_with_no_fits(db):
    """A segment with no fits at all does NOT appear in the summary —
    the list view is "show me what we know about", not "show me every
    segment". Empty-segment rendering is the caller's concern."""
    # Seed a second segment but write NO fits for it.
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s2', 'g1', 2, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()

    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))

    rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    assert [r["segment_id"] for r in rows] == ["s1"]


def test_iter_segment_fit_summaries_filters_by_kind(db):
    """A pool_fit on s1 should not appear in a segment_fit summary."""
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    pool_payload = {**_make_payload(n_attempts=999), "kind": "pool_fit"}
    db.save_segment_fit("s1", "pool_fit", pool_payload)

    seg_rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    pool_rows = list(db.iter_segment_fit_summaries("g1", kind="pool_fit"))
    assert [r["n_attempts"] for r in seg_rows] == [10]
    assert [r["n_attempts"] for r in pool_rows] == [999]
