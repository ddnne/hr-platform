-- Historical third-party files remain separate from live odds / Paper inputs.
INSERT OR IGNORE INTO source_control(source) VALUES ('keibaodds-history');
CREATE TABLE IF NOT EXISTS archive_jobs (
 day TEXT NOT NULL, page INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 PRIMARY KEY(day,page)
);
CREATE TABLE IF NOT EXISTS archive_attempts (
 event_id TEXT PRIMARY KEY, day TEXT NOT NULL, page INTEGER NOT NULL, status TEXT NOT NULL,
 scheduled_at TEXT NOT NULL, reserved_at TEXT NOT NULL, fetch_started_at TEXT, http_status INTEGER,
 headers_received_at TEXT, received_at TEXT, raw_saved_at TEXT, raw_sha256 TEXT, raw_bytes INTEGER,
 parsed_at TEXT, available_at TEXT, error_code TEXT, duration_ms REAL,
 source_updated_at TEXT, historical_available_at TEXT
);
