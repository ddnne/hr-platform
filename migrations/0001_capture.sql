CREATE TABLE IF NOT EXISTS source_control (
 source TEXT PRIMARY KEY, next_allowed_at INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, owner_event_id TEXT
);
INSERT OR IGNORE INTO source_control(source) VALUES ('nar-daily-odds');
CREATE TABLE IF NOT EXISTS captures (
 event_id TEXT PRIMARY KEY, scheduled_capture_at TEXT NOT NULL, fetch_started_at TEXT,
 status TEXT NOT NULL, http_status INTEGER, headers_received_at TEXT, collector_received_at TEXT, raw_saved_at TEXT,
 raw_sha256 TEXT, raw_bytes INTEGER, etag TEXT, validator_sent TEXT, validator_raw_sha256 TEXT,
 file_name TEXT, file_timestamp TEXT, duration_ms REAL, retry_after_at TEXT,
 parsed_at TEXT, available_at TEXT, error_code TEXT
);
CREATE TABLE IF NOT EXISTS raw_observations (
 observation_id TEXT PRIMARY KEY REFERENCES captures(event_id), raw_sha256 TEXT NOT NULL,
 received_at TEXT NOT NULL, raw_saved_at TEXT NOT NULL, evidence TEXT NOT NULL,
 dataset_kind TEXT NOT NULL, source_updated_at TEXT, source_published_at TEXT
);
CREATE INDEX IF NOT EXISTS observation_history ON raw_observations(received_at);
