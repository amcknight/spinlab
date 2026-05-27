"""Shared episode-grouping and decay-weight helpers.

Used by both `death_aware_rolling` and `bootstrap_resample` estimators.
Module-internal (leading `_`) — not part of the public estimators API,
but stable for in-package imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.models import EventAttempt


@dataclass
class _Episode:
    """Per-episode aggregated view used by estimator math layers.

    Produced by `_group_into_episodes` and consumed by estimators that
    need to reason about an episode as a whole (e.g. for episode-level
    aggregates, bootstrap resampling, or floor-over-episodes).
    """
    episode_id: str
    events: list["EventAttempt"]
    outcome: str       # "completed" if any event is survived, else "died"
    had_any_death: bool


def _group_into_episodes(events: list["EventAttempt"]) -> list[_Episode]:
    """Group events by episode_id, dropping any episode with an invalidated event.

    Episodes are returned in the chronological order their FIRST event arrived
    in the input list. The scheduler queries events via
    Database.get_segment_event_rows which returns rows ordered by row id
    (chronological insertion order), so the first occurrence of each
    episode_id reflects the episode's start time.

    Python dicts preserve insertion order (PEP 468 / 3.7+), so iterating
    by_id below yields episodes in their first-encounter order.
    """
    by_id: dict[str, list["EventAttempt"]] = {}
    for ev in events:
        by_id.setdefault(ev.episode_id, []).append(ev)

    episodes: list[_Episode] = []
    for ep_id, ev_list in by_id.items():
        if any(ev.invalidated for ev in ev_list):
            continue
        had_any_death = any(ev.outcome.value == "died" for ev in ev_list)
        any_survived = any(ev.outcome.value == "survived" for ev in ev_list)
        outcome = "completed" if any_survived else "died"
        episodes.append(_Episode(
            episode_id=ep_id, events=ev_list,
            outcome=outcome, had_any_death=had_any_death,
        ))
    return episodes


def _compute_weights(n_episodes: int, halflife: int) -> list[float]:
    """Return exponentially-decayed weights, one per episode.

    weights[i] = 2 ** (-(n_episodes - 1 - i) / halflife)

    The most-recent episode (index n_episodes - 1) has weight 1.0. An episode
    halflife steps back has weight 0.5.
    """
    return [
        2.0 ** (-(n_episodes - 1 - i) / halflife)
        for i in range(n_episodes)
    ]
