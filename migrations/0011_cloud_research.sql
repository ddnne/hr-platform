-- Research only: immutable definitions and fixed-cutoff replay, separate from Paper.
CREATE TABLE cloud_research_bundles (
  bundle_id TEXT PRIMARY KEY,
  version TEXT NOT NULL UNIQUE,
  engine_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  registered_at TEXT NOT NULL
);
CREATE TABLE cloud_research_jobs (
  job_id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES cloud_research_bundles(bundle_id),
  race_id TEXT NOT NULL,
  asof_at TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  owner TEXT,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  input_hash TEXT,
  result_hash TEXT,
  available_at TEXT,
  error_code TEXT
);
CREATE INDEX cloud_research_pending ON cloud_research_jobs(status,registered_at,job_id);
