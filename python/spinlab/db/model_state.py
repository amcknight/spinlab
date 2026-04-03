"""Model state, allocator config, and gold computation queries."""

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
        self.conn.commit()

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
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def load_all_model_states(self, game_id: str) -> list[ModelStateRow]:
        cur = self.conn.execute(
            """SELECT m.segment_id, m.estimator, m.state_json, m.output_json, m.updated_at
               FROM model_state m
               JOIN segments s ON m.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1""",
            (game_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

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
            result[d["segment_id"]].append(d)
        return result

    def compute_golds(self, game_id: str) -> dict[str, GoldRow]:
        """Compute gold times for all active segments in a game."""
        cur = self.conn.execute(
            """SELECT a.segment_id,
                      MIN(CASE WHEN a.completed = 1 THEN a.time_ms END) AS gold_ms,
                      MIN(CASE WHEN a.completed = 1 THEN a.clean_tail_ms END) AS clean_gold_ms
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               GROUP BY a.segment_id""",
            (game_id,),
        )
        result: dict[str, GoldRow] = {}
        for row in cur.fetchall():
            result[row[0]] = {"gold_ms": row[1], "clean_gold_ms": row[2]}
        return result

    def save_allocator_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO allocator_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def load_allocator_config(self, key: str) -> str | None:
        cur = self.conn.execute(
            "SELECT value FROM allocator_config WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def delete_allocator_config(self, key: str) -> None:
        self.conn.execute("DELETE FROM allocator_config WHERE key = ?", (key,))
        self.conn.commit()
