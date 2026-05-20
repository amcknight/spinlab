"""Tests for ``spinlab fit rebuild`` — cold-refit every fittable segment.

The point of rebuild is "I've collected a new dataset; recompute every
segment_fit from scratch under the current model version." Used for
revalidating a captured dataset against a newer model.

To keep the tests fast we monkeypatch ``fit_segment`` rather than call
JAX. The CLI's behavior we want to pin: wipe → iterate → persist, with
the right segments included/excluded.
"""
from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from spinlab.db import Database


def _write_cfg(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(
        f"data:\n  dir: {data_dir.as_posix()}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    return cfg, data_dir


def _seed(tmp_path: Path, events_per_segment: dict[str, int]) -> tuple[Path, Database]:
    """Seed one game with N segments + a controlled number of events each."""
    cfg, data_dir = _write_cfg(tmp_path)
    db = Database(data_dir / "spinlab.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) "
        "VALUES ('sess1', 'g1', '2026-05-19T00:00:00Z')"
    )
    for ordinal, (sid, n) in enumerate(events_per_segment.items(), start=1):
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, "
            "start_type, end_type, ordinal, active, created_at, updated_at) "
            "VALUES (?, 'g1', ?, 'entrance', 'exit', ?, 1, "
            "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
            (sid, ordinal, ordinal),
        )
        for i in range(n):
            outcome = "died" if i % 3 == 0 else "survived"
            db.conn.execute(
                """INSERT INTO attempts
                   (segment_id, session_id, episode_id, outcome, time_ms,
                    source, invalidated, created_at)
                   VALUES (?, 'sess1', ?, ?, ?, 'practice', 0,
                           '2026-05-19T00:00:00Z')""",
                (sid, f"ep_{sid}_{i}", outcome, 25000 + i * 100),
            )
    db.conn.commit()
    return cfg, db


def _fake_fit_segment(monkeypatch):
    """Replace the heavy JAX refit with a deterministic stub.

    The stub is registered under the public re-export
    (``spinlab.segments_model.fit_segment``) — that's what the rebuild
    CLI imports — and tracks (segment_id, n_attempts) per call so the
    test can assert on what got fit.
    """
    calls: list[tuple[str, int, dict]] = []

    def stub(attempts, *, segment_id, **kwargs):
        # Record kwargs so a test can assert that rebuild calls
        # `fit_segment` cold (no prev_result, etc.). `refit_segment`
        # is the warm-start API; rebuild MUST use `fit_segment` and
        # NOT pass prev_result — otherwise the cold-rebuild property
        # silently degrades to a warm refit chain.
        calls.append((segment_id, len(attempts), kwargs))
        return {
            "schema": "segments-v1", "kind": "segment_fit",
            "segment_id": segment_id, "n_attempts": len(attempts),
            "model": "haz1", "wall_time_s": 0.01,
            "status": {
                "converged": True, "band_source": "laplace",
                "laplace_pd": True, "ppc_tension": False, "fittable": True,
            },
            "result": {"derived": {
                "M_clear": {"median_ms": 25000, "p5_ms": 22000, "p95_ms": 28000},
            }},
            "caveats": [],
        }

    import spinlab.segments_model as sm
    monkeypatch.setattr(sm, "fit_segment", stub)
    # cli_fit_rebuild imports `from spinlab.segments_model import fit_segment`
    # lazily at run-time; patching the module attribute is enough.
    return calls


def _run(**ns_overrides) -> tuple[int, str]:
    from spinlab import cli_fit_rebuild
    buf = io.StringIO()
    ns = argparse.Namespace(**{
        "kind": "segment_fit",
        **ns_overrides,
    })
    with redirect_stdout(buf):
        code = cli_fit_rebuild.run(ns)
    return code, buf.getvalue()


def test_rebuild_refits_segments_at_or_above_floor(tmp_path, monkeypatch):
    cfg, _ = _seed(tmp_path, {"s_below": 3, "s_at_floor": 5, "s_above": 8})
    calls = _fake_fit_segment(monkeypatch)

    code, out = _run(config=str(cfg), game="g1")
    assert code == 0
    # Below-floor segment should NOT have been fit.
    refit_ids = {sid for sid, _n, _kw in calls}
    assert refit_ids == {"s_at_floor", "s_above"}
    # Counts passed match what we seeded.
    counts = {sid: n for sid, n, _kw in calls}
    assert counts["s_at_floor"] == 5
    assert counts["s_above"] == 8


def test_rebuild_calls_cold_fit_without_prev_result(tmp_path, monkeypatch):
    """Rebuild must use the cold ``fit_segment`` (no warm start), not
    pass ``prev_result``. Locks the cold-rebuild property against a
    future "let's just call refit_segment to share code" refactor that
    would silently degrade rebuild's semantics."""
    cfg, _ = _seed(tmp_path, {"s_above": 8})
    calls = _fake_fit_segment(monkeypatch)

    code, _ = _run(config=str(cfg), game="g1")
    assert code == 0
    assert len(calls) == 1
    _sid, _n, kwargs = calls[0]
    assert "prev_result" not in kwargs, (
        f"rebuild leaked prev_result into fit_segment call: {kwargs!r}"
    )


def test_rebuild_wipes_existing_segment_fit_rows(tmp_path, monkeypatch):
    """A second rebuild should leave only the freshly-written rows —
    not 2x the previous count."""
    cfg, db = _seed(tmp_path, {"s_above": 8})
    _fake_fit_segment(monkeypatch)

    code, _ = _run(config=str(cfg), game="g1")
    assert code == 0
    count_after_first = db.conn.execute(
        "SELECT COUNT(*) FROM segment_fits WHERE kind = 'segment_fit'"
    ).fetchone()[0]
    assert count_after_first == 1

    code, _ = _run(config=str(cfg), game="g1")
    assert code == 0
    count_after_second = db.conn.execute(
        "SELECT COUNT(*) FROM segment_fits WHERE kind = 'segment_fit'"
    ).fetchone()[0]
    # Still 1 — the wipe runs before the new fit lands.
    assert count_after_second == 1


def test_rebuild_does_not_touch_pool_fit_rows(tmp_path, monkeypatch):
    """A rebuild of segment_fit must not delete pool_fit rows — those
    are managed by `spinlab fit-pool` separately."""
    cfg, db = _seed(tmp_path, {"s_above": 8})
    pool_payload = {
        "schema": "segments-v1", "kind": "pool_fit",
        "segment_id": "s_above", "n_attempts": 8,
        "model": "haz1", "wall_time_s": 0.02,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {"derived": {}},
        "caveats": [],
    }
    db.save_segment_fit("s_above", "pool_fit", pool_payload)
    _fake_fit_segment(monkeypatch)

    code, _ = _run(config=str(cfg), game="g1")
    assert code == 0
    pool_count = db.conn.execute(
        "SELECT COUNT(*) FROM segment_fits WHERE kind = 'pool_fit'"
    ).fetchone()[0]
    assert pool_count == 1


def test_rebuild_skips_invalidated_events(tmp_path, monkeypatch):
    """Invalidated attempts shouldn't count toward the floor or get
    passed to fit_segment."""
    cfg, db = _seed(tmp_path, {"s1": 10})
    # Invalidate 6 of the 10.
    db.conn.execute(
        "UPDATE attempts SET invalidated = 1 "
        "WHERE segment_id = 's1' AND id IN ("
        "  SELECT id FROM attempts WHERE segment_id = 's1' LIMIT 6"
        ")"
    )
    db.conn.commit()

    calls = _fake_fit_segment(monkeypatch)
    code, _ = _run(config=str(cfg), game="g1")
    assert code == 0
    # 4 remaining events — below the floor — so no fit at all.
    assert calls == []


def test_rebuild_errors_when_game_not_found(tmp_path, monkeypatch):
    cfg, _ = _seed(tmp_path, {"s1": 5})
    _fake_fit_segment(monkeypatch)

    code, out = _run(config=str(cfg), game="nonexistent")
    assert code == 1
    assert "nonexistent" in out


def test_rebuild_succeeds_with_zero_eligible_segments(tmp_path, monkeypatch):
    """A game with segments but no segment above the floor exits 0 with
    an informational message — nothing to do isn't an error."""
    cfg, _ = _seed(tmp_path, {"s1": 2, "s2": 3})
    calls = _fake_fit_segment(monkeypatch)

    code, out = _run(config=str(cfg), game="g1")
    assert code == 0
    assert calls == []
    assert "nothing to do" in out.lower() or "0 segments" in out.lower() \
        or "no eligible" in out.lower()


def test_rebuild_handles_missing_fits_extra(tmp_path, monkeypatch):
    """If [fits] isn't installed, exit 2 with an install hint."""
    cfg, _ = _seed(tmp_path, {"s1": 5})
    # Force the import inside `run` to fail.
    import sys
    monkeypatch.setitem(sys.modules, "spinlab.segments_model", None)

    code, out = _run(config=str(cfg), game="g1")
    assert code == 2
    assert "[fits]" in out
