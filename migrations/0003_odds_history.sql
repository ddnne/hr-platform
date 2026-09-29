-- Bodies remain in R2. Parsing does not change collection status or source controls.
CREATE TABLE IF NOT EXISTS odds_parses (
 parse_id TEXT PRIMARY KEY,
 observation_id TEXT NOT NULL REFERENCES raw_observations(observation_id),
 version TEXT NOT NULL, encoding TEXT NOT NULL,
 status TEXT NOT NULL, parsed_at TEXT NOT NULL, available_at TEXT,
 body_hash TEXT, error_code TEXT,
 UNIQUE(observation_id, version, encoding)
);
CREATE TABLE IF NOT EXISTS odds_races (
 parse_id TEXT NOT NULL REFERENCES odds_parses(parse_id), race_id TEXT NOT NULL, markets TEXT NOT NULL,
 PRIMARY KEY(parse_id, race_id)
);
CREATE INDEX IF NOT EXISTS odds_race_history ON odds_races(race_id, parse_id);
CREATE INDEX IF NOT EXISTS odds_parse_revisions ON odds_parses(observation_id, available_at, parse_id);
