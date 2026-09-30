"""Prospectively enrolled, receipt-based shadow Paper inputs.

The raw parsers remain unqualified. This policy explicitly assumes that a horse
listed in two fresh official sources, with no change or a recognized jockey-only
change, is a candidate runner. The latter label is recognized from state parser
v6 onward, subject to parse availability. It does not establish actual starters,
market synchronization or a sales channel deadline. Unknown update times stay null.
"""

from copy import deepcopy
from datetime import timedelta
import json
from .common import canonical, identity, instant, seconds, stamp
from .race_metadata import MetadataEvidence
from .race_state import StateEvidence

POLICY = {
    "version": "observed-entries-v1",
    "runner_assumption": "LISTED_NOT_REPORTED_WITHDRAWN",
    "max_metadata_age_seconds": 300,
    "max_state_age_seconds": 300,
    "pre_race_assumption": "OBSERVED_CLOCK_BEFORE_KNOWN_SCHEDULE_WITHOUT_RESULTS",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_plans(
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, experiment TEXT NOT NULL, race_id TEXT NOT NULL,
 available_at TEXT, body TEXT NOT NULL);
"""


def configuration(base):
    if base["mode"] != "EXPLORATORY_SHADOW" or "input_policy" in base:
        raise ValueError("PROSPECTIVE_SHADOW_ONLY")
    return {**deepcopy(base), "version": base["version"] + ":" + POLICY["version"],
            "input_policy": deepcopy(POLICY)}


def validate_policy(config):
    if config["mode"] != "EXPLORATORY_SHADOW" or config.get("input_policy") != POLICY:
        raise ValueError("PROSPECTIVE_POLICY")


def cutoff(schedule, config):
    return stamp((instant(schedule["scheduled_start_at"])
                  - timedelta(seconds=config["asof_before_start_seconds"])).isoformat())


def metadata_reason(race_id, evidence):
    if not evidence or evidence["status"] == "QUARANTINED" or not evidence.get("metadata"):
        return "METADATA_MISSING"
    race = evidence["metadata"]
    if "帯広" in race_id or race["surface_label"] not in {"芝", "ダート"}:
        return "OUT_OF_SCOPE"
    if race["result_present"]:
        return "RESULT_PRESENT"
    if not race["scheduled_start_at"]:
        return "SCHEDULE_NOT_KNOWN"
    if not race["entry_count_matches"] or not 3 <= len(race["horses"]) <= 16:
        return "ENTRY_COUNT_MISMATCH"
    return None


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
    at = cutoff(schedule, config)
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
    # Replay sees only the plan revision published by the historical cutoff.
    # tick selects today's current plan independently before calling decide.
    plan = current_plan(store, identity([config["version"], race_id]), at)
    if (not plan or plan["config"] != config or plan["schedule"] != schedule
        or plan["registered_at"] >= stamp(at) or plan["asof_at"] != stamp(at)):
        return {**view, "reason": "PROSPECTIVE_PLAN_REQUIRED"}
    meta = MetadataEvidence(store).for_race(race_id, at)
    state = StateEvidence(store).asof(race_id, at)
    view["state_evidence"] = state
    view["plan_revision_id"] = plan["revision_id"]
    view["metadata_evidence"] = meta
    view["input_policy"] = config["input_policy"]
    reason = metadata_reason(race_id, meta["evidence"])
    page = state["evidence"]
    if not reason and (not page or page["status"] == "QUARANTINED"):
        reason = "STATE_MISSING"
    if not reason and (not 0 <= meta["age_seconds"] <= POLICY["max_metadata_age_seconds"]
                       or not 0 <= state["age_seconds"] <= POLICY["max_state_age_seconds"]):
        reason = "STATE_OR_METADATA_STALE"
    if not reason:
        race = meta["evidence"]["metadata"]
        starts = [race["scheduled_start_at"], page["scheduled_start_at"]]
        if any(stamp(start) != schedule["scheduled_start_at"] for start in starts):
            reason = "SCHEDULE_CHANGED"
        elif page["odds_stage"] != "CLOCK_DISPLAYED":
            reason = "RACE_NOT_PRE_RACE"
        elif set(page["runners"]) != set(race["horses"]):
            reason = "ENTRY_COUNT_MISMATCH"
        elif any(r["status"] not in {"NO_CHANGE_DISPLAYED", "JOCKEY_CHANGED"}
                 for r in page["runners"].values()):
            reason = "RUNNER_CHANGE_OR_UNKNOWN"
        elif seconds(schedule["scheduled_start_at"], at) <= 0:
            reason = "RACE_NOT_PRE_RACE"
    view["reason"] = reason or view["reason"]
    if view["reason"]:
        return view
    # Build a derived input only. Never reparse the odds with hindsight state or
    # change either raw parser's active=None / source_updated_at=None values.
    proof = {name: {k: item["evidence"][k] for k in
                   ("id", "observation_id", "raw_hash", "received_at", "available_at")}
             for name, item in (("metadata", meta), ("state", state))}
    derived = {
        "race_id": race_id, "discipline": "FLAT",
        "surface": {"ダート": "DIRT", "芝": "TURF"}[race["surface_label"]],
        "runners": sorted(map(int, race["horses"])),
        "status": "PRE_RACE", "pre_race_evidence": proof,
        "runner_assumption": POLICY["runner_assumption"], "input_policy": POLICY["version"],
        "schedule_version": schedule["version"],
        "known_at": max(meta["evidence"]["available_at"], page["available_at"]),
    }
    for item in view["markets"].values():
        # Explicit final/void evidence supplied with odds always takes precedence.
        observed = item["content"]["state"]
        if observed.get("status") not in {"UNKNOWN", "PRE_RACE"}:
            return {**view, "reason": "RACE_NOT_PRE_RACE"}
        item["observed_state"] = observed
        item["observed_body_hash"] = item["body_hash"]
        item["content"] = {**item["content"], "state": derived}
        item["state_hash"] = identity(derived)
        item["body_hash"] = store.body(canonical(item["content"]), "normalized")
    view["assembled_at"] = stamp(store.clock())
    view["research_assumptions"] = [POLICY["runner_assumption"], POLICY["pre_race_assumption"]]
    return view


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
