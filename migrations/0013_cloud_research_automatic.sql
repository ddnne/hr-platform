-- Prospective research uses the existing job queue. No second Paper ledger.
ALTER TABLE cloud_research_jobs ADD COLUMN schedule_hash TEXT;
CREATE INDEX cloud_research_race_jobs ON cloud_research_jobs(bundle_id,race_id,status);
CREATE TABLE cloud_research_automatic (
  slot INTEGER PRIMARY KEY CHECK(slot=1),
  bundle_id TEXT NOT NULL REFERENCES cloud_research_bundles(bundle_id),
  from_date TEXT NOT NULL,
  activated_at TEXT NOT NULL,
  schedule_owner TEXT,
  schedule_lease_until TEXT,
  evaluation_cursor TEXT NOT NULL DEFAULT '',
  evaluation_checked_at TEXT
);
