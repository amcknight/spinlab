-- segments-v07 Phase 1: persistent storage for v1 JSON fit payloads.
--
-- Each row is one fit. (segment_id, kind, fitted_at) is the natural
-- key, but we keep an INTEGER PRIMARY KEY for cheap "most recent"
-- lookups by id. `payload_json` is the full v1 envelope; status
-- columns are projected out for SQL-side filtering (the inspector
-- wants "show me unfittable segments" without parsing every blob).

CREATE TABLE IF NOT EXISTS segment_fits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  kind TEXT NOT NULL CHECK (kind IN ('segment_fit', 'pool_fit')),
  n_attempts INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  -- Status columns projected from the JSON envelope. NULLABLE because
  -- a non-converged envelope omits most of these (`band_source='none'`
  -- but `fittable` etc. still come through; defensive null tolerance
  -- avoids future-payload-shape lockout).
  band_source TEXT,
  fittable INTEGER,
  ppc_tension INTEGER,
  wall_time_ms INTEGER,  -- payload's wall_time_s * 1000, for SLO tracking
  fitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segment_fits_segment_kind_id
  ON segment_fits(segment_id, kind, id DESC);
