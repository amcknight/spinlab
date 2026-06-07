"""Model state, allocator config, and gold computation queries."""

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import TypedDict


class ModelStateRow(TypedDict):
    segment_id: str
    estimator: str
    state_json: str | None
    output_json: str | None
    updated_at: str


class GoldRow(TypedDict):
    gold_ms: int | None
    clean_gold_ms: int | None


class ModelStateMixin:
    """Estimator state persistence and allocator config."""
    conn: sqlite3.Connection

    def save_model_state(
        self, segment_id: str, estimator: str, state_json: str, output_json: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO model_state (segment_id, estimator, state_json, output_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(segment_id, estimator) DO UPDATE SET
                 state_json=excluded.state_json,
                 output_json=excluded.output_json,
                 updated_at=excluded.updated_at""",
            (segment_id, estimator, state_json, output_json, now),
        )

    def load_model_state(self, segment_id: str, estimator: str | None = None) -> ModelStateRow | None:
        if estimator:
            cur = self.conn.execute(
                "SELECT segment_id, estimator, state_json, output_json, updated_at "
                "FROM model_state WHERE segment_id = ? AND estimator = ?",
                (segment_id, estimator),
            )
        else:
            cur = self.conn.execute(
                "SELECT segment_id, estimator, state_json, output_json, updated_at "
                "FROM model_state WHERE segment_id = ? LIMIT 1",
                (segment_id,),
            )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "segment_id": row[0], "estimator": row[1], "state_json": row[2],
            "output_json": row[3], "updated_at": row[4],
        }

    def load_all_model_states_for_segment(self, segment_id: str) -> list[ModelStateRow]:
        """Load all estimator states for a single segment."""
        cur = self.conn.execute(
            "SELECT segment_id, estimator, state_json, output_json, updated_at "
            "FROM model_state WHERE segment_id = ?",
            (segment_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]

    def load_all_model_states(self, game_id: str) -> list[ModelStateRow]:
        cur = self.conn.execute(
            """SELECT m.segment_id, m.estimator, m.state_json, m.output_json, m.updated_at
               FROM model_state m
               JOIN segments s ON m.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1""",
            (game_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]

    def load_all_model_states_for_game(self, game_id: str) -> dict[str, list[ModelStateRow]]:
        """Load all estimator states for all active segments in a game."""
        cur = self.conn.execute(
            """SELECT m.segment_id, m.estimator, m.state_json, m.output_json, m.updated_at
               FROM model_state m
               JOIN segments s ON m.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1""",
            (game_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        result: dict[str, list[ModelStateRow]] = defaultdict(list)
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            result[d["segment_id"]].append(d)  # type: ignore[arg-type]
        return result

    def compute_golds(self, game_id: str) -> dict[str, GoldRow]:
        """Compute gold (best) total + clean times for all active segments.

        Post-Phase-0 the ``attempts`` table is event-level, so we can't take
        ``MIN(time_ms)`` in SQL — the episode total is ``sum(event time_ms) +
        deaths*penalty``. We delegate to ``get_all_attempts_by_segment`` which
        runs the same episode roll-up the estimators use, and pick the min
        per segment in Python. Scale (50 segments × hundreds of attempts) is
        cheap enough that the trip through Python is not worth optimizing.
        """
        # Inline import to avoid a cycle: AttemptsMixin is composed into the
        # Database class alongside this mixin and pulls in the model layer.
        attempts_by_seg = self.get_all_attempts_by_segment(game_id)  # type: ignore[attr-defined]
        result: dict[str, GoldRow] = {}
        for seg_id, rows in attempts_by_seg.items():
            gold_ms: int | None = None
            clean_gold_ms: int | None = None
            for row in rows:
                if not row["completed"]:
                    continue
                # Invalidated attempts (user marked them bad / mis-bounded) must
                # not win gold. The estimator's Floor already excludes these via
                # attempts_from_rows; Best (gold_ms) was the one path that still
                # let them through. Without this, the invalidate button has no
                # effect on the Best column.
                if row["invalidated"]:
                    continue
                t = row["time_ms"]
                if t is not None and (gold_ms is None or t < gold_ms):
                    gold_ms = t
                c = row["clean_tail_ms"]
                if c is not None and (clean_gold_ms is None or c < clean_gold_ms):
                    clean_gold_ms = c
            result[seg_id] = {"gold_ms": gold_ms, "clean_gold_ms": clean_gold_ms}
        return result

    def save_allocator_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO allocator_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def load_allocator_config(self, key: str) -> str | None:
        cur = self.conn.execute(
            "SELECT value FROM allocator_config WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def delete_allocator_config(self, key: str) -> None:
        self.conn.execute("DELETE FROM allocator_config WHERE key = ?", (key,))

    def purge_stale_model_state(self, keep_estimator: str = "em_suite_sampler") -> None:
        """Drop derived rows for any other estimator and the obsolete
        estimator-selection / per-estimator-tuning config keys.

        Event data (``attempts``) is the source of truth and is never touched
        here. The kept estimator's rows rebuild from events on the next
        scheduler tick.
        """
        self.conn.execute(
            "DELETE FROM model_state WHERE estimator != ?", (keep_estimator,),
        )
        self.conn.execute("DELETE FROM allocator_config WHERE key = 'estimator'")
        self.conn.execute(
            "DELETE FROM allocator_config WHERE key LIKE 'estimator_params:%'",
        )
        self.conn.commit()
