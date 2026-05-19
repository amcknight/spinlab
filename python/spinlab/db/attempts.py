"""Attempt queries — event-level on disk, episode-shaped at the API boundary.

After the segments-v07 Phase 0 refactor the ``attempts`` table holds one row
per died/survived event. Estimators and the UI still consume episode-shaped
rows (deaths count, total time_ms, clean_tail_ms), so every read path here
groups events by ``episode_id`` and rolls them back up. Writes have two
flavors:

  * ``log_event_attempt(EventAttempt)`` — primitive writer, one event = one
    row. Used by the production capture pipeline (PracticeTiming,
    SpeedRunTiming, reference recording).
  * ``log_attempt(Attempt)`` — episode-shaped convenience writer. Splits the
    episode into ``deaths + 1`` synthesized event rows for test fixtures and
    seed helpers that pre-date the event model.

The synthesis math in ``log_attempt`` and the roll-up math in
``get_segment_attempts``/``get_recent_attempts`` use a shared
``death_penalty_ms`` constant; keeping the two consistent is what lets the
legacy adapter return identical ``AttemptRow`` values to what the old
episode-shaped table stored.
"""

import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import TypedDict

from ..models import (
    DEFAULT_DEATH_PENALTY_MS,
    Attempt,
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
)


class AttemptRow(TypedDict):
    segment_id: str
    completed: int
    time_ms: int | None
    deaths: int
    clean_tail_ms: int | None
    created_at: str
    invalidated: int


class RecentAttemptRow(TypedDict, total=False):
    id: int
    segment_id: str
    session_id: str | None
    capture_run_id: str | None
    completed: int
    time_ms: int | None
    source: str
    deaths: int
    clean_tail_ms: int | None
    created_at: str
    chosen_allocator: str | None
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int


class EventAttemptRow(TypedDict):
    """Raw event row, exposed for tests and the Phase 1 segments-model adapter."""
    id: int
    segment_id: str
    session_id: str | None
    capture_run_id: str | None
    episode_id: str
    outcome: str
    time_ms: int
    source: str
    chosen_allocator: str | None
    invalidated: int
    created_at: str


RECENT_ATTEMPTS_DB_LIMIT = 5


def _roll_up_episode(
    events: Sequence[sqlite3.Row | dict],
    death_penalty_ms: int,
) -> dict:
    """Convert a chronological event list for one episode into an episode dict.

    ``events`` rows can be sqlite Row objects or plain dicts; both are indexed
    by column name. The output dict mirrors the legacy AttemptRow / recent-
    attempt shape so the rest of the app (estimators, routes, UI) sees the
    same fields it always has.

    The shared ``death_penalty_ms`` MUST match the value used at insert time
    in ``log_attempt`` — together they round-trip the episode-total time_ms.
    """
    deaths = sum(1 for e in events if e["outcome"] == AttemptOutcome.DIED.value)
    last = events[-1]
    completed = last["outcome"] == AttemptOutcome.SURVIVED.value
    raw_sum = sum(e["time_ms"] for e in events)
    # The synth shim writes 0-time died events to satisfy NOT NULL when the
    # original episode lacked a meaningful time. Treat an all-zero incomplete
    # episode as time_ms=None to preserve the legacy "no recorded time" signal.
    if completed:
        # Penalty rebuilds the legacy episode total: raw wall-clock + 3.2s per death.
        time_ms: int | None = raw_sum + death_penalty_ms * deaths
        clean_tail_ms: int | None = last["time_ms"]
    elif raw_sum > 0:
        time_ms = raw_sum
        clean_tail_ms = None
    else:
        time_ms = None
        clean_tail_ms = None
    invalidated = any(int(e["invalidated"]) for e in events)
    return {
        "segment_id": last["segment_id"],
        "session_id": last["session_id"],
        "capture_run_id": last["capture_run_id"],
        "completed": int(completed),
        "time_ms": time_ms,
        "source": last["source"],
        "deaths": deaths,
        "clean_tail_ms": clean_tail_ms,
        "created_at": last["created_at"],
        "chosen_allocator": last["chosen_allocator"],
        "invalidated": int(invalidated),
        "episode_id": last["episode_id"],
        # The closing event id is useful as a stable per-episode identifier
        # for the UI (used as the row key in Recent Attempts).
        "id": last["id"],
    }


def _split_episode_into_events(
    attempt: Attempt, death_penalty_ms: int,
) -> list[EventAttempt]:
    """Synthesize event rows from an episode-shaped Attempt.

    Used only by the ``log_attempt`` shim (test fixtures, seed helpers).
    Production code calls ``log_event_attempt`` directly per event.

    Synthesis math:
      * Completed episodes split (total - deaths*penalty - clean_tail) evenly
        across the deaths; the surviving event takes ``clean_tail_ms`` (or
        the full time_ms when ``clean_tail_ms`` is unset).
      * Incomplete episodes synthesize one died event per recorded death; if
        the episode recorded no deaths, one synthetic died event keeps the
        FK row alive (the schema requires ``time_ms NOT NULL``).
      * The split is even because the estimators don't read per-death times —
        ``_roll_up_episode`` only surfaces deaths *count* and episode totals.
    """
    episode_id = uuid.uuid4().hex
    created_at = attempt.created_at
    common = {
        "segment_id": attempt.segment_id,
        "episode_id": episode_id,
        "session_id": attempt.session_id,
        "capture_run_id": attempt.capture_run_id,
        "source": attempt.source if isinstance(attempt.source, AttemptSource) else AttemptSource(attempt.source),
        "chosen_allocator": attempt.chosen_allocator,
        "invalidated": attempt.invalidated,
        "created_at": created_at,
    }

    if not attempt.completed:
        death_count = max(attempt.deaths, 1)
        # When time_ms is None (production aborts) every synthesized death
        # gets 0ms — the legacy adapter surfaces time_ms=None back. When
        # callers passed a time_ms anyway (some tests do), split it evenly
        # across the synthesized deaths so the round-trip preserves it.
        total = attempt.time_ms or 0
        base = total // death_count
        remainder = total - base * death_count
        return [
            EventAttempt(
                outcome=AttemptOutcome.DIED,
                time_ms=int(base + (remainder if i == 0 else 0)),
                **common,
            )
            for i in range(death_count)
        ]

    assert attempt.time_ms is not None, "completed attempt requires time_ms"
    deaths = attempt.deaths
    clean_tail = attempt.clean_tail_ms if attempt.clean_tail_ms is not None else attempt.time_ms
    if deaths == 0:
        return [EventAttempt(
            outcome=AttemptOutcome.SURVIVED, time_ms=int(clean_tail), **common,
        )]

    raw_remaining = attempt.time_ms - death_penalty_ms * deaths - clean_tail
    # The remaining wall-clock for the death phase. Negative would imply the
    # caller produced inconsistent fields (clean_tail > time_ms - penalty);
    # we clamp at 0 rather than asserting — round-trip math still holds
    # because the adapter recomputes total from the synthesized rows.
    raw_remaining = max(raw_remaining, 0)
    base = raw_remaining // deaths
    remainder = raw_remaining - base * deaths

    events: list[EventAttempt] = []
    for i in range(deaths):
        events.append(EventAttempt(
            outcome=AttemptOutcome.DIED,
            time_ms=int(base + (remainder if i == 0 else 0)),
            **common,
        ))
    events.append(EventAttempt(
        outcome=AttemptOutcome.SURVIVED, time_ms=int(clean_tail), **common,
    ))
    return events


class AttemptsMixin:
    """Attempt logging and statistics."""
    conn: sqlite3.Connection

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def log_event_attempt(self, event: EventAttempt) -> int:
        """Persist one per-event row. Primitive writer used by production."""
        cur = self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, capture_run_id, episode_id, outcome,
                time_ms, source, chosen_allocator, invalidated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.segment_id, event.session_id, event.capture_run_id,
                event.episode_id, event.outcome.value,
                event.time_ms,
                event.source.value if isinstance(event.source, AttemptSource) else event.source,
                event.chosen_allocator, int(event.invalidated),
                event.created_at.isoformat(),
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]  # always set after INSERT

    def log_attempt(
        self, attempt: Attempt,
        death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    ) -> int:
        """Episode-shaped writer — synthesizes event rows and persists each.

        Kept for test fixtures and seed helpers that pre-date the event-level
        schema. Returns the row id of the *closing* event in the synthesized
        sequence so callers that key off ``lastrowid`` still get a stable id.
        """
        events = _split_episode_into_events(attempt, death_penalty_ms)
        last_id = 0
        for ev in events:
            last_id = self.log_event_attempt(ev)
        return last_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_segment_attempt_count(self, segment_id: str, session_id: str) -> int:
        """Count *episodes* on a segment in a specific practice/speed-run session.

        Pre-Phase-0 this counted rows directly; post-Phase-0 rows are events,
        so we count distinct episode_ids to preserve the episode semantics
        every caller of this method depends on (segments_attempted,
        regression tests, etc.).
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT episode_id) AS cnt FROM attempts "
            "WHERE segment_id = ? AND session_id = ?",
            (segment_id, session_id),
        ).fetchone()
        return row["cnt"]

    def get_recent_attempts(
        self, game_id: str, limit: int = RECENT_ATTEMPTS_DB_LIMIT,
        session_id: str | None = None,
        death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    ) -> list[RecentAttemptRow]:
        """Last N *episodes* for ``game_id``, most recent first.

        Episodes are ranked by the id of their closing (max-id) event. Each
        returned dict matches the legacy ``RecentAttemptRow`` shape so the
        frontend's Recent Attempts panel stays unchanged.
        """
        where = "s.game_id = ?"
        params: list = [game_id]
        if session_id:
            where += " AND a.session_id = ?"
            params.append(session_id)
        # Pull the closing-event id per episode, ranked by recency, then
        # widen back to full event lists for those episodes. The two-step
        # avoids dragging the join across every event we'll throw away.
        params2 = list(params) + [limit]
        episode_rows = self.conn.execute(
            f"""SELECT a.episode_id AS episode_id, MAX(a.id) AS last_id
                FROM attempts a JOIN segments s ON a.segment_id = s.id
                WHERE {where}
                GROUP BY a.episode_id
                ORDER BY last_id DESC
                LIMIT ?""",
            params2,
        ).fetchall()
        if not episode_rows:
            return []
        episode_ids = [r["episode_id"] for r in episode_rows]
        placeholders = ",".join("?" * len(episode_ids))
        ev_rows = self.conn.execute(
            f"""SELECT a.*, s.description, s.level_number,
                       s.start_type, s.start_ordinal,
                       s.end_type, s.end_ordinal
                FROM attempts a JOIN segments s ON a.segment_id = s.id
                WHERE a.episode_id IN ({placeholders})
                ORDER BY a.id""",
            episode_ids,
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in ev_rows:
            grouped[row["episode_id"]].append(row)
        results: list[RecentAttemptRow] = []
        # Preserve the ranking from episode_rows (most-recent closer first).
        for ep_row in episode_rows:
            events = grouped[ep_row["episode_id"]]
            episode = _roll_up_episode(events, death_penalty_ms)
            # Carry the segment-join columns from the closing event's row.
            closing = events[-1]
            episode["description"] = closing["description"]
            episode["level_number"] = closing["level_number"]
            episode["start_type"] = closing["start_type"]
            episode["start_ordinal"] = closing["start_ordinal"]
            episode["end_type"] = closing["end_type"]
            episode["end_ordinal"] = closing["end_ordinal"]
            results.append(episode)  # type: ignore[arg-type]
        return results

    def get_segment_attempts(
        self, segment_id: str,
        death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    ) -> list[AttemptRow]:
        """All *episodes* for a segment, ordered by chronological close.

        Returned shape matches the legacy AttemptRow TypedDict so the
        scheduler / state_builder / model-routes pipeline is unaffected.
        """
        rows = self.conn.execute(
            "SELECT * FROM attempts WHERE segment_id = ? "
            "ORDER BY episode_id, id",
            (segment_id,),
        ).fetchall()
        return _group_episodes_to_attempt_rows(rows, death_penalty_ms)

    def get_all_attempts_by_segment(
        self, game_id: str,
        death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    ) -> dict[str, list[AttemptRow]]:
        """Load all episodes for all active segments in a game."""
        rows = self.conn.execute(
            """SELECT a.* FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY a.segment_id, a.episode_id, a.id""",
            (game_id,),
        ).fetchall()
        result: dict[str, list[AttemptRow]] = defaultdict(list)
        # Two-level group: segment_id -> episode_id -> [events].
        by_seg: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_seg[row["segment_id"]].append(row)
        for seg_id, seg_rows in by_seg.items():
            result[seg_id] = _group_episodes_to_attempt_rows(seg_rows, death_penalty_ms)
        return result

    def get_segment_event_rows(
        self, segment_id: str, session_id: str | None = None,
    ) -> list[EventAttemptRow]:
        """Raw per-event rows for ``segment_id``, in insertion order.

        Bypasses the episode roll-up. Tests and the Phase 1 segments-model
        adapter use this; production code should prefer ``get_segment_attempts``.
        """
        where = "segment_id = ?"
        params: list = [segment_id]
        if session_id is not None:
            where += " AND session_id = ?"
            params.append(session_id)
        rows = self.conn.execute(
            f"SELECT * FROM attempts WHERE {where} ORDER BY id", params,
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def set_attempt_invalidated(self, attempt_id: int, invalidated: bool) -> None:
        """Mark the *episode* containing ``attempt_id`` invalidated.

        Episode-level semantics are what the UI exposes (a player invalidates
        a death-out episode, not an individual death event). We expand from
        the given event id to its full episode and flip every row's flag.
        """
        ep_row = self.conn.execute(
            "SELECT episode_id FROM attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        if ep_row is None:
            return
        self.conn.execute(
            "UPDATE attempts SET invalidated = ? WHERE episode_id = ?",
            (int(invalidated), ep_row["episode_id"]),
        )

    def get_last_practice_attempt(self, session_id: str) -> int | None:
        """Closing event id of the most-recent episode in ``session_id``.

        Callers (UI invalidation flow) treat the returned id as "the most
        recent attempt"; returning the closing event id keeps the episode-
        level semantics they actually want.
        """
        row = self.conn.execute(
            """SELECT MAX(id) AS id FROM attempts
               WHERE session_id = ?
               GROUP BY episode_id
               ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        return row["id"] if row else None

    def attempt_exists(self, attempt_id: int) -> bool:
        """True if any event row with this id exists."""
        row = self.conn.execute(
            "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        return row is not None


def _group_episodes_to_attempt_rows(
    rows: list[sqlite3.Row], death_penalty_ms: int,
) -> list[AttemptRow]:
    """Sort events by episode and roll each up into an AttemptRow."""
    by_episode: dict[str, list[sqlite3.Row]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        ep = row["episode_id"]
        if ep not in by_episode:
            order.append(ep)
        by_episode[ep].append(row)
    out: list[AttemptRow] = []
    # Rank episodes by their closing-event id so chronological order is
    # preserved across insert interleavings (rare but possible during
    # multi-segment sessions).
    order.sort(key=lambda ep: max(r["id"] for r in by_episode[ep]))
    for ep in order:
        episode = _roll_up_episode(by_episode[ep], death_penalty_ms)
        out.append({  # type: ignore[typeddict-item]
            "segment_id": episode["segment_id"],
            "completed": episode["completed"],
            "time_ms": episode["time_ms"],
            "deaths": episode["deaths"],
            "clean_tail_ms": episode["clean_tail_ms"],
            "created_at": episode["created_at"],
            "invalidated": episode["invalidated"],
        })
    return out
