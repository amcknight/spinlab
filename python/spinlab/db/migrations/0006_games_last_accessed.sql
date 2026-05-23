-- Track the last time each game was loaded, so the dashboard can surface
-- recently played games at the top of the ROM selector.
ALTER TABLE games ADD COLUMN last_accessed TEXT;

-- Backfill: treat first-seen (created_at) as a reasonable last-accessed proxy
-- for existing rows so they appear in the recently-played list immediately.
UPDATE games SET last_accessed = created_at WHERE last_accessed IS NULL;
