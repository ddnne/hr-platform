-- Derived evaluations never rewrite the frozen research input/result or Paper.
CREATE TABLE cloud_research_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES cloud_research_jobs(job_id),
  version TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  available_at TEXT NOT NULL
);
CREATE INDEX cloud_research_evaluation_history ON cloud_research_evaluations(job_id,available_at,evaluation_id);
