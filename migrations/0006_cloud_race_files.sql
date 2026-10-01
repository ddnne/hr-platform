-- Race list, entrants and results are versioned snapshots; empty result fields do not prove pre-race.
CREATE TABLE IF NOT EXISTS race_file_parses (
 parse_id TEXT PRIMARY KEY,
 observation_id TEXT NOT NULL REFERENCES raw_observations(observation_id),
 version TEXT NOT NULL, encoding TEXT NOT NULL,
 status TEXT NOT NULL, parsed_at TEXT NOT NULL, available_at TEXT,
 body_hash TEXT, error_code TEXT,
 UNIQUE(observation_id, version, encoding)
);
CREATE TABLE IF NOT EXISTS race_file_races (
 parse_id TEXT NOT NULL REFERENCES race_file_parses(parse_id), race_id TEXT NOT NULL, markets TEXT NOT NULL,
 PRIMARY KEY(parse_id, race_id)
);
CREATE INDEX IF NOT EXISTS race_file_history ON race_file_races(race_id, parse_id);
CREATE INDEX IF NOT EXISTS race_file_revisions ON race_file_parses(observation_id, available_at, parse_id);

-- One parser lease per observation/version; collection never waits for it.
CREATE TABLE IF NOT EXISTS normalization_jobs (
 observation_id TEXT NOT NULL REFERENCES raw_observations(observation_id), version TEXT NOT NULL,
 owner TEXT NOT NULL, lease_until TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(observation_id,version)
);
