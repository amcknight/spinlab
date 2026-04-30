"""Waypoint CRUD."""

import sqlite3

from ..models import Waypoint


class WaypointsMixin:
    conn: sqlite3.Connection
    def upsert_waypoint(self, w: Waypoint) -> None:
        self.conn.execute(
            """INSERT INTO waypoints
               (id, game_id, level_number, endpoint_type, ordinal, conditions_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (w.id, w.game_id, w.level_number, w.endpoint_type,
             w.ordinal, w.conditions_json),
        )
        self.conn.commit()

    def get_waypoint(self, waypoint_id: str) -> Waypoint | None:
        row = self.conn.execute(
            """SELECT id, game_id, level_number, endpoint_type, ordinal, conditions_json
               FROM waypoints WHERE id = ?""",
            (waypoint_id,),
        ).fetchone()
        if row is None:
            return None
        return Waypoint(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            endpoint_type=row["endpoint_type"],
            ordinal=row["ordinal"],
            conditions_json=row["conditions_json"],
        )
