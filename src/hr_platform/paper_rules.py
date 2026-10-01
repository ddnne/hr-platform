"""Storage-independent Paper eligibility, selection and monetary settlement rules.

Callers own clocks, evidence provenance, idempotent records and publication.
These rules neither access a database nor turn unqualified evidence into finality.
"""
import math

from .common import MODEL_PROBABILITY_FIELDS, paper_asof, seconds, stamp, instant
from .parser import MARKETS


def eligibility(view, config, schedule, decision_at, *, race_id=None):
    if view["reason"]:
        return view["reason"]
    asof = view["asof_at"]
    if (
        stamp(schedule["known_at"]) > asof
        or paper_asof(schedule, config, race_id) != asof
    ):
        return "SCHEDULE_NOT_KNOWN"
    if seconds(decision_at, asof) < 0 or seconds(decision_at, asof) > config["max_decision_delay_seconds"]:
        return "DECISION_TOO_LATE"
    if schedule.get("sales_close_at") and stamp(decision_at) >= stamp(schedule["sales_close_at"]):
        return "DECISION_TOO_LATE"
    snapshots = list(view["markets"].values())
    if len({x["state_hash"] for x in snapshots}) != 1:
        return "RACE_STATE_CHANGED"
    for x in snapshots:
        state = x["content"]["state"]
        if state.get("discipline") != "FLAT" or state.get("surface") not in {"DIRT", "TURF"}:
            return "OUT_OF_SCOPE"
        if state.get("status") != "PRE_RACE" or not state.get("pre_race_evidence"):
            return "RACE_NOT_PRE_RACE"
        if state.get("schedule_version") != schedule["version"]:
            return "RACE_STATE_CHANGED"
        if stamp(state["known_at"]) > asof:
            return "RACE_STATE_CHANGED"
        source = x["content"].get("source_updated_at")
        if source and (
            seconds(x["received_at"], source) < 0 or seconds(asof, source) > config["max_age_seconds"]
        ):
            return "STALE"
    times = [x["content"].get("source_updated_at") for x in snapshots]
    if all(times):
        if seconds(max(times, key=instant), min(times, key=instant)) > config["max_source_skew_seconds"]:
            return "ASYNCHRONOUS"
    elif len({x["observation_id"] for x in snapshots}) != 1:
        return "ASYNCHRONOUS"
    return None


def select(rows, model, tolerance):
    field = MODEL_PROBABILITY_FIELDS[model]
    candidates = []
    for row in rows:
        edge = row[field] * row["odds"] - 1
        if row["valid_log"] and row[field] > row["v_target"] and edge > 0:
            candidates.append((edge, row))
    if not candidates:
        return None
    best = max(edge for edge, _ in candidates)
    return min((row for edge, row in candidates if best - edge <= tolerance), key=lambda r: r["selection"])


def settlement_values(decision, payout):
    """Calculate only from normalized payout/refund evidence; pending stays null."""
    rows = payout.get("tickets", [])
    seen = set()
    for r in rows:
        k = (r["market"], r["selection"])
        if k in seen:
            raise ValueError("DUPLICATE_PAYOUT_TICKET")
        seen.add(k)
        for field in ("payout_per_100", "refund_per_100"):
            x = r.get(field, 0)
            if not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
                raise ValueError("PAYOUT_AMOUNT")
    # Explicit normalized evidence only: never infer special payout from zero odds.
    special = {}
    for item in payout.get("special_payouts", []):
        market, amount = item["market"], item["payout_per_100"]
        if market not in MARKETS.values() or market in special:
            raise ValueError("SPECIAL_PAYOUT_MARKET")
        if type(amount) is not int or amount not in {70, 80}:
            raise ValueError("SPECIAL_PAYOUT_AMOUNT")
        special[market] = amount
    if special and payout.get("void"):
        raise ValueError("SPECIAL_PAYOUT_CONFLICT")
    for row in rows:
        if row["market"] in special and (
            row.get("payout_per_100", 0) != 0 or row.get("refund_per_100", 0) not in {0, 100}
        ):
            raise ValueError("SPECIAL_PAYOUT_CONFLICT")
    stake = decision["stake_yen"]
    paid = refund = 0
    special_paid = False
    status = "NO_BET" if stake == 0 else "PENDING"
    if stake and payout.get("final") and decision["target"] in payout.get("complete_markets", []):
        if payout.get("void"):
            refund = stake
        else:
            if decision["target"] in special:
                paid = special[decision["target"]] * stake / 100
                special_paid = True
            for r in rows:
                if (r["market"], r["selection"]) == (decision["target"], decision["selection"]):
                    refund = r.get("refund_per_100", 0) * stake / 100
                    if special_paid:
                        if refund:
                            paid = 0
                            special_paid = False
                    else:
                        paid = r.get("payout_per_100", 0) * stake / 100
        status = "SETTLED"
    return {
        "status": status,
        "stake_yen": stake,
        "payout_yen": paid if status != "PENDING" else None,
        "special_payout_yen": (paid if special_paid else 0) if status != "PENDING" else None,
        "refund_yen": refund if status != "PENDING" else None,
        "profit_yen": paid + refund - stake if status != "PENDING" else None,
        "roi": (paid + refund - stake) / stake if stake and status == "SETTLED" else None,
    }


def validate_paper_config(config):
    if config["mode"] not in {"EXPLORATORY_SHADOW", "FROZEN_PAPER"} or config["stake_yen"] != 100:
        raise ValueError("PAPER_CONFIG")
    if config["max_tickets_per_race"] != 1 or config["target"] in config["references"]:
        raise ValueError("PAPER_CONFIG")
    reference_policy = config.get("reference_constraint_policy", "require_feasible")
    if reference_policy not in {"require_feasible", "allow_inconsistent_shadow"} or (
        reference_policy == "allow_inconsistent_shadow" and config["mode"] != "EXPLORATORY_SHADOW"
    ):
        raise ValueError("REFERENCE_CONSTRAINT_POLICY")
    return reference_policy


def model_error_reason(error):
    return 'DATA_MISSING' if str(error) in {'INCOMPLETE_MARKET', 'UNUSABLE_ODDS', 'REFERENCE_MISSING'} else 'MODEL_ERROR'
