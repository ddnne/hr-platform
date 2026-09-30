"""Retrospective diagnostics with an explicit information cutoff; never place Paper bets."""

from .common import identity, paper_asof, seconds, stamp
from .model import analyze, ModelError
from .paper import eligibility


def trajectory(store, race_id, markets, at):
    cutoff = stamp(at)
    histories = {}
    excluded = {}
    for market in markets:
        # Resolve the observation's current parse before looking for a market:
        # deleted markets/races must not reappear in path features.
        points = [x for x in store.history(race_id, market, cutoff, current_only=True)
                  if x["received_at"] <= cutoff]
        for item in points:
            item["age_seconds"] = seconds(cutoff, item["received_at"])
        histories[market] = []
        excluded[market] = []
        for item in points:
            if item["event"]["dataset_kind"] == "FINAL_ONLY":
                reason = "FINAL_ONLY"
            elif item["content"]["state"].get("status") != "PRE_RACE":
                reason = "RACE_NOT_PRE_RACE"
            else:
                reason = None
            if reason:
                excluded[market].append({**item, "exclusion_reason": reason})
            else:
                histories[market].append(item)
    return {
        "asof_at": cutoff,
        "race_id": race_id,
        "history": histories,
        "excluded_points": excluded,
        "gaps": store.gaps(cutoff),
        "gap_scope": "STORE_CAPTURE_PLAN_NOT_RACE_SPECIFIC",
        "interpolated": False,
        "market_synchronization_verified": False,
    }


def research_asof(store, race_id, schedule, config, analyzer=analyze):
    if config["target"] in config["references"]:
        raise ValueError("TARGET_IN_REFERENCES")
    cutoff = paper_asof(schedule, config, race_id)
    markets = list(dict.fromkeys([config["target"], *config["references"]]))
    view = store.asof(race_id, markets, cutoff, config["max_age_seconds"])
    # Evaluate historical input quality only. This is not evidence that computation
    # completed before the real decision deadline; no historical decision is written.
    reason = eligibility(view, config, schedule, cutoff, race_id=race_id)
    report = {
        "status": "INPUT_INELIGIBLE" if reason else "ANALYZED",
        "reason": reason,
        "race_id": race_id,
        "config": config,
        "config_hash": identity(config),
        "schedule": schedule,
        "schedule_provenance": "CALLER_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED",
        "asof_at": cutoff,
        "view": view,
        "trajectory": trajectory(store, race_id, markets, cutoff),
        "purpose": "RETROSPECTIVE_MARKET_DIAGNOSTIC_NOT_BACKTEST",
        "live_execution_verified": False,
        "profitability_verified": False,
        "paper_decision_created": False,
    }
    if reason is None:
        content = {market: item["content"] for market, item in view["markets"].items()}
        try:
            result = analyzer(content[config["target"]]["state"]["runners"], content, config)
            report["analysis"] = result
            if result["identification"]["status"] == "INCONSISTENT":
                report.update(status="REFERENCE_INCONSISTENT", reason="INCONSISTENT")
        except ModelError:
            report.update(status="MODEL_ERROR", reason="MODEL_ERROR")
    report["analyzed_at"] = stamp(store.clock())
    return report
