CREATE TABLE IF NOT EXISTS cloud_paper_experiments (
 experiment TEXT PRIMARY KEY, config_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_paper_plans (
 plan_id TEXT PRIMARY KEY, experiment TEXT NOT NULL, race_id TEXT NOT NULL, day TEXT NOT NULL,
 asof_at TEXT NOT NULL, plan_body TEXT NOT NULL, registered_at TEXT NOT NULL,
 owner TEXT, lease_until TEXT, decision_at TEXT, details_hash TEXT, decisions TEXT, settlement_checked_at TEXT,
 UNIQUE(experiment,race_id)
);
CREATE TABLE IF NOT EXISTS cloud_paper_plan_revisions (
 revision_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES cloud_paper_plans(plan_id),
 plan_body TEXT NOT NULL, registered_at TEXT NOT NULL
);
-- Keep the current schedule and its history in the same atomic write.
CREATE TRIGGER IF NOT EXISTS cloud_paper_initial_plan AFTER INSERT ON cloud_paper_plans
BEGIN
 INSERT INTO cloud_paper_plan_revisions VALUES(json_extract(NEW.plan_body,'$.revision_id'),NEW.plan_id,NEW.plan_body,NEW.registered_at);
END;
CREATE TRIGGER IF NOT EXISTS cloud_paper_revised_plan AFTER UPDATE OF plan_body ON cloud_paper_plans
WHEN NEW.plan_body != OLD.plan_body
BEGIN
 INSERT INTO cloud_paper_plan_revisions VALUES(json_extract(NEW.plan_body,'$.revision_id'),NEW.plan_id,NEW.plan_body,NEW.registered_at);
END;
CREATE TABLE IF NOT EXISTS cloud_paper_settlements (
 plan_id TEXT NOT NULL REFERENCES cloud_paper_plans(plan_id), evidence_id TEXT NOT NULL,
 body_hash TEXT NOT NULL, available_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,evidence_id)
);
