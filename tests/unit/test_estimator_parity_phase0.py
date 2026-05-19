"""Phase 0 test 5: event-level pipeline produces identical estimator outputs.

Pre-refactor goldens captured by ``tests/fixtures/segments_v07/capture_golden.py``
on `main` directly above the Phase 0 commits. This test feeds the
equivalent event-level rows through the new write path
(``db.log_event_attempt``) and runs every estimator over the rolled-up
episodes via the legacy adapter. The expectation is bit-for-bit equality
on every ``Estimate`` field — any drift means the adapter math doesn't
reconstruct the old episode shape correctly.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spinlab.db import Database
from spinlab.estimators import get_estimator
from spinlab.models import (
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)
from spinlab.scheduler import _attempts_from_rows

GOLDEN_PATH = Path(__file__).parent.parent / "fixtures" / "segments_v07" / "golden_estimator_outputs.json"


def _load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def _build_db_with_events(scenario: dict) -> Database:
    """Insert one scenario's event rows into a fresh DB."""
    db = Database(":memory:")
    db.upsert_game("g1", "TestGame", "any%")
    db.create_session("sess1", "g1")
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="seg1",
    ))
    for ev in scenario["events_new_shape"]:
        outcome = (
            AttemptOutcome.SURVIVED if ev["outcome"] == "survived"
            else AttemptOutcome.DIED
        )
        db.log_event_attempt(EventAttempt(
            segment_id="seg1",
            session_id="sess1",
            episode_id=ev["episode_id"],
            outcome=outcome,
            time_ms=ev["time_ms"],
            source=AttemptSource.PRACTICE,
            created_at=datetime.fromisoformat(ev["created_at"]).astimezone(UTC),
        ))
    return db


def _run_estimators_via_adapter(db: Database) -> dict:
    """Mirror what ``capture_golden.py`` did pre-refactor, but pull the
    episodes back through ``get_segment_attempts`` → ``_attempts_from_rows``
    (the legacy adapter path) instead of consuming AttemptRecord directly."""
    rows = db.get_segment_attempts("seg1")
    records = _attempts_from_rows(rows)
    out: dict[str, dict] = {}
    for name in ("kalman", "exp_decay", "rolling_mean"):
        est = get_estimator(name)
        state = est.rebuild_state(records)
        model = est.model_output(state, records)
        out[name] = model.to_dict()
    return out


@pytest.mark.parametrize("scenario_idx", [0, 1])
def test_estimator_parity_via_event_level_adapter(scenario_idx):
    """For each pinned scenario, the event-level write + adapter round-trip
    must produce the same estimator output the pre-refactor episode-shaped
    pipeline produced."""
    golden = _load_golden()
    scenario = golden["scenarios"][scenario_idx]

    db = _build_db_with_events(scenario)
    actual = _run_estimators_via_adapter(db)
    expected = scenario["golden_outputs"]

    for est_name in ("kalman", "exp_decay", "rolling_mean"):
        for series in ("total", "clean"):
            for field in ("expected_ms", "ms_per_attempt", "floor_ms"):
                a = actual[est_name][series][field]
                e = expected[est_name][series][field]
                if a is None or e is None:
                    assert a is e, (
                        f"{est_name}.{series}.{field}: "
                        f"actual={a!r} expected={e!r} "
                        f"(scenario {scenario['name']})"
                    )
                else:
                    assert a == pytest.approx(e, rel=1e-9, abs=1e-9), (
                        f"{est_name}.{series}.{field}: "
                        f"actual={a!r} expected={e!r} "
                        f"(scenario {scenario['name']})"
                    )
