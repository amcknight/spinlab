"""Capture pre-refactor estimator outputs for the segments-v07 Phase 0 parity test.

Run once on `main`'s code before the event-level attempts refactor lands; the
output JSON gets pinned in `tests/test_scheduler.py::test_estimator_parity_*`
and post-refactor must reproduce it bit-for-bit when fed an equivalent
event-level attempt sequence.

Scenarios are defined twice:
  * `episodes_old_shape` — the AttemptRecord rows the current pipeline
    consumes directly (time_ms includes the per-death penalty math).
  * `events_new_shape` — the per-event rows the post-refactor pipeline will
    consume. The legacy adapter rolls these back up into an AttemptRecord
    that must be IDENTICAL to `episodes_old_shape` for the same scenario.

Run:
    python tests/fixtures/segments_v07/capture_golden.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from spinlab.estimators import get_estimator
from spinlab.models import AttemptRecord

DEATH_PENALTY_MS = 3200  # PracticeSession default; matches what arm()s in production.


@dataclass
class EpisodeOldShape:
    """The pre-refactor view of an episode — what's fed to AttemptRecord today."""
    completed: bool
    time_ms: int | None       # total including penalty
    deaths: int
    clean_tail_ms: int | None
    created_at: str


@dataclass
class EventNewShape:
    """The post-refactor per-event row that the adapter will roll up."""
    episode_id: str
    outcome: str              # 'died' | 'survived'
    time_ms: int              # raw wall-clock for this event
    created_at: str


def _verify_equivalent(
    episodes: list[EpisodeOldShape],
    events: list[EventNewShape],
    death_penalty_ms: int,
) -> None:
    """The adapter spec: roll events up and check it produces the old shape.

    Inlined here as a sanity check on the scenario definitions — it's the same
    logic the test will exercise post-refactor through the real adapter.
    """
    by_ep: dict[str, list[EventNewShape]] = {}
    order: list[str] = []
    for ev in events:
        if ev.episode_id not in by_ep:
            by_ep[ev.episode_id] = []
            order.append(ev.episode_id)
        by_ep[ev.episode_id].append(ev)

    # created_at is not consumed by the estimators we care about, so the
    # equivalence check ignores it and we only compare the numerical fields.
    def _compare_key(e: EpisodeOldShape) -> tuple:
        return (e.completed, e.time_ms, e.deaths, e.clean_tail_ms)

    rebuilt: list[EpisodeOldShape] = []
    for ep_id in order:
        ev_list = by_ep[ep_id]
        deaths = sum(1 for e in ev_list if e.outcome == "died")
        last = ev_list[-1]
        completed = last.outcome == "survived"
        total_raw = sum(e.time_ms for e in ev_list)
        time_ms: int | None
        if completed:
            time_ms = total_raw + death_penalty_ms * deaths
            clean_tail_ms: int | None = last.time_ms  # wall-clock of the surviving event
        else:
            time_ms = None
            clean_tail_ms = None
        rebuilt.append(EpisodeOldShape(
            completed=completed,
            time_ms=time_ms,
            deaths=deaths,
            clean_tail_ms=clean_tail_ms,
            created_at=last.created_at,
        ))

    if [_compare_key(r) for r in rebuilt] != [_compare_key(e) for e in episodes]:
        raise AssertionError(
            "scenario definition is inconsistent — event roll-up != episode shape:\n"
            f"  events     -> {rebuilt}\n"
            f"  expected   -> {episodes}"
        )


def _run_estimators(episodes: list[EpisodeOldShape]) -> dict:
    """Feed AttemptRecord list through each estimator's rebuild_state + model_output."""
    records = [
        AttemptRecord(
            time_ms=e.time_ms,
            completed=e.completed,
            deaths=e.deaths,
            clean_tail_ms=e.clean_tail_ms,
            created_at=e.created_at,
        )
        for e in episodes
    ]
    out: dict[str, dict] = {}
    for name in ("kalman", "exp_decay", "rolling_mean"):
        est = get_estimator(name)
        state = est.rebuild_state(records)
        model = est.model_output(state, records)
        out[name] = model.to_dict()
    return out


def main() -> None:
    scenarios = []

    # ---- Scenario A: five clean attempts, smooth improvement ----
    a_eps = [
        EpisodeOldShape(True, 30000, 0, 30000, "2026-05-18T00:00:00+00:00"),
        EpisodeOldShape(True, 28000, 0, 28000, "2026-05-18T00:00:30+00:00"),
        EpisodeOldShape(True, 26500, 0, 26500, "2026-05-18T00:01:00+00:00"),
        EpisodeOldShape(True, 25500, 0, 25500, "2026-05-18T00:01:30+00:00"),
        EpisodeOldShape(True, 25000, 0, 25000, "2026-05-18T00:02:00+00:00"),
    ]
    a_events = [
        EventNewShape("epA1", "survived", 30000, "2026-05-18T00:00:00+00:00"),
        EventNewShape("epA2", "survived", 28000, "2026-05-18T00:00:30+00:00"),
        EventNewShape("epA3", "survived", 26500, "2026-05-18T00:01:00+00:00"),
        EventNewShape("epA4", "survived", 25500, "2026-05-18T00:01:30+00:00"),
        EventNewShape("epA5", "survived", 25000, "2026-05-18T00:02:00+00:00"),
    ]
    _verify_equivalent(a_eps, a_events, DEATH_PENALTY_MS)
    scenarios.append({
        "name": "five_clean_attempts",
        "death_penalty_ms": DEATH_PENALTY_MS,
        "episodes_old_shape": [e.__dict__ for e in a_eps],
        "events_new_shape": [e.__dict__ for e in a_events],
        "golden_outputs": _run_estimators(a_eps),
    })

    # ---- Scenario B: mixed deaths + clears, three completed episodes ----
    # Episode 1: clean clear
    # Episode 2: 2 deaths then clear (penalty applied)
    # Episode 3: 1 death then clear
    # Episode 4: 1 death then abort (incomplete — completed=False)
    # Episode 5: clean clear (final)
    b_eps = [
        EpisodeOldShape(True, 30000, 0, 30000, "2026-05-18T00:10:00+00:00"),
        # 2 deaths (8s + 7s raw) + survive 20s → raw 35s + 2*3200 penalty = 41400
        EpisodeOldShape(True, 41400, 2, 20000, "2026-05-18T00:10:30+00:00"),
        # 1 death (6s raw) + survive 22s → raw 28s + 3200 = 31200
        EpisodeOldShape(True, 31200, 1, 22000, "2026-05-18T00:11:00+00:00"),
        # Aborted attempt: 1 death + 4s of play, no survival — incomplete row
        EpisodeOldShape(False, None, 1, None, "2026-05-18T00:11:30+00:00"),
        EpisodeOldShape(True, 27000, 0, 27000, "2026-05-18T00:12:00+00:00"),
    ]
    b_events = [
        EventNewShape("epB1", "survived", 30000, "2026-05-18T00:10:00+00:00"),
        EventNewShape("epB2", "died", 8000, "2026-05-18T00:10:30+00:00"),
        EventNewShape("epB2", "died", 7000, "2026-05-18T00:10:38+00:00"),
        EventNewShape("epB2", "survived", 20000, "2026-05-18T00:10:58+00:00"),
        EventNewShape("epB3", "died", 6000, "2026-05-18T00:11:00+00:00"),
        EventNewShape("epB3", "survived", 22000, "2026-05-18T00:11:06+00:00"),
        EventNewShape("epB4", "died", 4000, "2026-05-18T00:11:30+00:00"),
        # No survived event for epB4 — adapter must emit completed=False.
        EventNewShape("epB5", "survived", 27000, "2026-05-18T00:12:00+00:00"),
    ]
    _verify_equivalent(b_eps, b_events, DEATH_PENALTY_MS)
    scenarios.append({
        "name": "mixed_deaths_and_aborts",
        "death_penalty_ms": DEATH_PENALTY_MS,
        "episodes_old_shape": [e.__dict__ for e in b_eps],
        "events_new_shape": [e.__dict__ for e in b_events],
        "golden_outputs": _run_estimators(b_eps),
    })

    out = {
        "schema_version": 1,
        "captured_on_branch": "main (pre-event-level-refactor)",
        "scenarios": scenarios,
    }
    path = Path(__file__).parent / "golden_estimator_outputs.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
