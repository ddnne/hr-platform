"""Shared prospective input qualification; evidence storage is owned by callers."""
from copy import deepcopy
from .common import identity, seconds, stamp

POLICY = {
    "version": "observed-entries-v1",
    "runner_assumption": "LISTED_NOT_REPORTED_WITHDRAWN",
    "max_metadata_age_seconds": 300,
    "max_state_age_seconds": 300,
    "pre_race_assumption": "OBSERVED_CLOCK_BEFORE_KNOWN_SCHEDULE_WITHOUT_RESULTS",
}


def configuration(base):
    if base["mode"] != "EXPLORATORY_SHADOW" or "input_policy" in base:
        raise ValueError("PROSPECTIVE_SHADOW_ONLY")
    return {**deepcopy(base), "version": base["version"] + ":" + POLICY["version"],
            "input_policy": deepcopy(POLICY)}


def validate_policy(config):
    if config["mode"] != "EXPLORATORY_SHADOW" or config.get("input_policy") != POLICY:
        raise ValueError("PROSPECTIVE_POLICY")


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


def qualify(view, meta, state, plan, race_id, schedule, config, at):
    validate_policy(config)
    if (not plan or plan["config"] != config or plan["schedule"] != schedule
        or plan["registered_at"] >= stamp(at) or plan["asof_at"] != stamp(at)):
        return {**view, "reason": "PROSPECTIVE_PLAN_REQUIRED"}
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
        item["body_hash"] = identity(item["content"])
    view["research_assumptions"] = [POLICY["runner_assumption"], POLICY["pre_race_assumption"]]
    return view
