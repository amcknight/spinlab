-- "Science"/strat-hunting attempts: written + counted for floor/PB, but
-- excluded from the expected-time/reliability model (effort wasn't max).
-- Mirrors `invalidated` but with the OPPOSITE floor behavior.
ALTER TABLE attempts ADD COLUMN experimental INTEGER NOT NULL DEFAULT 0;
