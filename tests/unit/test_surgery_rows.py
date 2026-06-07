from spinlab.routes.attempts import surgery_rows


def _att(id, created_at, clean_tail_ms, time_ms, deaths, completed, invalidated):
    # Mirrors the dict shape returned by Database.get_segment_attempts.
    return {
        "id": id, "created_at": created_at, "clean_tail_ms": clean_tail_ms,
        "time_ms": time_ms, "deaths": deaths, "completed": completed,
        "invalidated": invalidated,
    }


def test_surgery_rows_assigns_chronological_order_and_floor():
    # Out-of-order input; order must be by created_at ascending (1-based).
    raw = [
        _att(50, "2026-01-01T00:02:00+00:00", 13000, 19000, 1, 1, 0),
        _att(10, "2026-01-01T00:00:00+00:00", 15000, 15000, 0, 1, 0),
        _att(30, "2026-01-01T00:01:00+00:00", 11000, 11000, 0, 1, 0),  # floor
    ]
    rows = surgery_rows(raw)
    by_id = {r["id"]: r for r in rows}
    assert by_id[10]["order"] == 1
    assert by_id[30]["order"] == 2
    assert by_id[50]["order"] == 3
    assert by_id[30]["is_floor"] is True
    assert by_id[10]["is_floor"] is False
    assert by_id[50]["is_floor"] is False
    assert by_id[50]["total_ms"] == 19000
    assert by_id[10]["invalidated"] is False


def test_surgery_rows_floor_ignores_invalidated_and_incomplete():
    raw = [
        _att(1, "2026-01-01T00:00:00+00:00", 8000, 8000, 0, 1, 1),   # faster but INVALID
        _att(2, "2026-01-01T00:01:00+00:00", 9000, 9000, 0, 0, 0),   # faster but INCOMPLETE
        _att(3, "2026-01-01T00:02:00+00:00", 12000, 12000, 0, 1, 0),  # the real floor
    ]
    rows = surgery_rows(raw)
    by_id = {r["id"]: r for r in rows}
    assert by_id[3]["is_floor"] is True
    assert by_id[1]["is_floor"] is False
    assert by_id[2]["is_floor"] is False


def test_surgery_rows_incomplete_has_none_clean_tail_no_floor():
    raw = [_att(1, "2026-01-01T00:00:00+00:00", None, 9000, 2, 0, 0)]
    rows = surgery_rows(raw)
    assert rows[0]["clean_tail_ms"] is None
    assert rows[0]["is_floor"] is False
