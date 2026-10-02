-- Other sports share immutable raw storage, never the NAR/Paper normalization tables.
CREATE INDEX IF NOT EXISTS observation_kind_history ON raw_observations(dataset_kind,received_at,observation_id);
CREATE TABLE IF NOT EXISTS sports_parses (
 observation_id TEXT NOT NULL REFERENCES raw_observations(observation_id), parser_version TEXT NOT NULL,
 sport TEXT NOT NULL, race_id TEXT NOT NULL, resource_id TEXT NOT NULL,
 parsed_at TEXT NOT NULL, available_at TEXT NOT NULL, status TEXT NOT NULL,
 normalized_key TEXT, error_code TEXT,
 PRIMARY KEY(observation_id,parser_version)
);
CREATE INDEX IF NOT EXISTS sports_asof ON sports_parses(sport,race_id,available_at,observation_id);
