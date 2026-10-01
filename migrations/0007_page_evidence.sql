CREATE TABLE IF NOT EXISTS page_capture_plans (
 event_id TEXT PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL,
 url TEXT NOT NULL, race_id TEXT NOT NULL, registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_parses (
 parse_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES raw_observations(observation_id),
 kind TEXT NOT NULL, race_id TEXT NOT NULL, version TEXT NOT NULL,
 parsed_at TEXT NOT NULL, available_at TEXT, body_hash TEXT NOT NULL,
 UNIQUE(observation_id,version)
);
CREATE INDEX IF NOT EXISTS page_evidence_history ON page_parses(kind,race_id,available_at);
