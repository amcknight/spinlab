"""SpinLab scheduler — SM-2 adapted for speedrun practice sessions."""

from datetime import datetime
from typing import Optional

from .db import Database
from .models import Schedule, Rating, Split, SplitCommand


class Scheduler:
    """Picks the next split to practice based on SM-2 spaced repetition."""

    def __init__(self, db: Database, game_id: str, base_interval: float = 5.0):
        self.db = db
        self.game_id = game_id
        self.base_interval = base_interval

    def pick_next(self) -> Optional[SplitCommand]:
        """Select the next split to practice.

        Priority:
        1. Most overdue split (next_review <= now, ordered by oldest first)
        2. Split due soonest (even if not yet due — don't waste time waiting)
        3. Worst performing split (fallback)
        """
        now = datetime.utcnow()

        # 1. Overdue splits
        due = self.db.get_due_splits(self.game_id, now)
        if due:
            return self._split_dict_to_command(due[0])

        # 2. Next due (serve early rather than idle)
        upcoming = self.db.get_next_due(self.game_id)
        if upcoming:
            return self._split_dict_to_command(upcoming)

        return None

    def peek_next_n(self, n: int) -> list[str]:
        """Return the next N split IDs in priority order, without side effects.

        Used by the dashboard to show the upcoming queue.
        """
        now = datetime.utcnow()

        # Overdue first, then upcoming
        due = self.db.get_due_splits(self.game_id, now)
        if len(due) >= n:
            return [d["id"] for d in due[:n]]

        ids = [d["id"] for d in due]

        # Fill remaining from upcoming (all splits ordered by next_review)
        if len(ids) < n:
            for sid in self.db.get_all_scheduled_split_ids(self.game_id):
                if sid not in ids:
                    ids.append(sid)
                    if len(ids) >= n:
                        break

        return ids[:n]

    def process_rating(self, split_id: str, rating: Rating) -> None:
        """Update schedule based on player's rating."""
        if rating == Rating.SKIP:
            return

        row = self.db.conn.execute(
            "SELECT * FROM schedule WHERE split_id = ?", (split_id,)
        ).fetchone()

        if not row:
            return

        schedule = Schedule(
            split_id=split_id,
            ease_factor=row["ease_factor"],
            interval_minutes=row["interval_minutes"],
            repetitions=row["repetitions"],
            next_review=datetime.fromisoformat(row["next_review"]),
        )

        schedule.update(rating, self.base_interval)
        self.db.update_schedule(schedule)

    def reset_strat(self, split_id: str) -> None:
        """Reset a split's schedule for a strat change."""
        self.db.increment_strat_version(split_id)
        self.db.reset_schedule(split_id)

    def init_schedules(self) -> None:
        """Ensure every active split has a schedule entry."""
        splits = self.db.get_active_splits(self.game_id)
        for split in splits:
            self.db.ensure_schedule(split.id)

    @staticmethod
    def auto_rate_from_time(
        time_ms: int, reference_ms: int, completed: bool
    ) -> Rating:
        """Derive a rating from completion time vs reference.

        Used for optional passive-mode auto-rating.
        """
        if not completed:
            return Rating.AGAIN

        ratio = time_ms / reference_ms
        if ratio <= 1.0:
            return Rating.EASY
        elif ratio <= 1.15:
            return Rating.GOOD
        elif ratio <= 1.3:
            return Rating.HARD
        else:
            return Rating.AGAIN

    @staticmethod
    def _split_dict_to_command(d: dict) -> SplitCommand:
        ef = d.get("ease_factor") or 2.5
        reps = d.get("repetitions") or 0
        if reps == 0:
            difficulty = 0  # new — no signal yet
        elif ef < 1.8:
            difficulty = 1  # struggling
        elif ef < 2.5:
            difficulty = 2  # normal
        else:
            difficulty = 3  # strong
        return SplitCommand(
            id=d["id"],
            state_path=d["state_path"] or "",
            goal=d["goal"],
            description=d["description"] or "",
            reference_time_ms=d["reference_time_ms"],
            difficulty=difficulty,
        )
