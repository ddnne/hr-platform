"""Prospectively enrolled, receipt-based shadow Paper inputs.

The raw parsers remain unqualified. This policy explicitly assumes that a horse
listed in two fresh official sources, with no change or a recognized jockey-only
change, is a candidate runner. The latter label is recognized from state parser
v6 onward, subject to parse availability. It does not establish actual starters,
market synchronization or a sales channel deadline. Unknown update times stay null.
"""

import json
from .common import canonical, identity, paper_asof, stamp
from .race_metadata import MetadataEvidence
from .race_state import StateEvidence
from .prospective_rules import POLICY as POLICY, configuration, validate_policy, metadata_reason, qualify

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_plans(
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, experiment TEXT NOT NULL, race_id TEXT NOT NULL,
 available_at TEXT, body TEXT NOT NULL);
"""


def current_plan(store, plan_id, at=None):
    until = stamp(at) if at else "9999-12-31T23:59:59.999999+00:00"
    row = store.db.execute("""SELECT * FROM paper_plans WHERE plan_id=? AND available_at<=?
                            ORDER BY available_at DESC,id DESC LIMIT 1""", (plan_id, until)).fetchone()
    if not row:
        return None
    plan = json.loads(row["body"])
    return {**plan, "registered_at": row["available_at"],
            "status": "ENROLLED" if row["available_at"] < plan["asof_at"] else "REGISTRATION_TOO_LATE"}


def enroll(store, race_id, base_config):
    """Record a future race and schedule before looking at a decision-time price."""
    return register_plan(store, race_id, configuration(base_config))


def register_plan(store, race_id, config):
    from .paper import register_experiment

    validate_policy(config)
    store.db.executescript(SCHEMA)
    register_experiment(store, config)
    key = identity([config["version"], race_id])
    old = current_plan(store, key)
    decided = store.db.execute("SELECT 1 FROM decisions WHERE experiment=? AND race_id=?",
                               (config["version"], race_id)).fetchone()
    if decided:
        if not old:
            raise ValueError("DECISION_ALREADY_FIXED")
        return old
    now = stamp(store.clock())
    meta = MetadataEvidence(store).for_race(race_id, now)
    evidence = meta["evidence"]
    reason = metadata_reason(race_id, evidence)
    if reason:
        if old:
            return old  # The cutoff input checks will record the missing/invalid evidence.
        raise ValueError(reason)
    # Enrollment fixes intent only; fresh metadata/state are required again at
    # the cutoff. Older known schedules can be enrolled while still in the future.
    start = stamp(evidence["metadata"]["scheduled_start_at"])
    if old and old["schedule"]["scheduled_start_at"] == start:
        return old
    schedule = {"version": identity([race_id, start]), "scheduled_start_at": start,
                "known_at": evidence["available_at"], "sales_close_at": None}
    at = paper_asof(schedule, config, race_id)
    if not old and now >= at:
        raise ValueError("PROSPECTIVE_ENROLLMENT_TOO_LATE")
    revision = identity([key, evidence["id"], schedule])
    plan = {"id": key, "revision_id": revision, "race_id": race_id,
            "supersedes": old["revision_id"] if old else None,
            "change_reason": "SCHEDULE_CHANGED" if old else "INITIAL_ENROLLMENT",
            "asof_at": at, "schedule": schedule,
            "metadata_parse_id": evidence["id"], "config": config}
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO paper_plans VALUES(?,?,?,?,NULL,?)",
                         (revision, key, config["version"], race_id, canonical(plan).decode()))
    # The whole intent is durable before taking its publication time. A slow
    # INSERT/commit cannot turn post-cutoff intent into an earlier registration.
    available = stamp(store.clock())
    if available < now:
        raise ValueError("PROSPECTIVE_CLOCK_ORDER")
    with store.db:
        store.db.execute("UPDATE paper_plans SET available_at=? WHERE id=? AND available_at IS NULL",
                         (available, revision))
    return current_plan(store, key)


def build_view(store, race_id, schedule, config, at):
    validate_policy(config)
    store.db.executescript(SCHEMA)
    view = store.asof(race_id, [config["target"], *config["references"]], at, config["max_age_seconds"])
    plan = current_plan(store, identity([config["version"], race_id]), at)
    result = qualify(view, MetadataEvidence(store).for_race(race_id, at),
                     StateEvidence(store).asof(race_id, at), plan, race_id, schedule, config, at)
    if not result["reason"]:
        for item in result["markets"].values():
            assert item["body_hash"] == store.body(canonical(item["content"]), "normalized")
        result["assembled_at"] = stamp(store.clock())
    return result


def tick(store, plan_id):
    from .paper import decide

    store.db.executescript(SCHEMA)
    plan = current_plan(store, plan_id)
    if not plan:
        raise ValueError("PROSPECTIVE_PLAN_REQUIRED")
    # Update a not-yet-decided race from the schedule known now. A new cutoff
    # already in the past is retained as a missed revision, never backfilled.
    plan = register_plan(store, plan["race_id"], plan["config"])
    return {"plan_id": plan_id, "plan_status": plan["status"], "plan_revision_id": plan["revision_id"],
            "decisions": decide(store, plan["race_id"], plan["schedule"], plan["config"])}
