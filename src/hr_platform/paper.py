import json
from datetime import timedelta
import math
from .common import canonical, identity, instant, stamp, seconds
from .model import analyze, ModelError


def eligibility(view, config, schedule, decision_at):
    if view["reason"]:
        return view["reason"]
    asof = view["asof_at"]
    if (
        stamp(schedule["known_at"]) > asof
        or stamp(
            (
                instant(schedule["scheduled_start_at"])
                - timedelta(seconds=config["asof_before_start_seconds"])
            ).isoformat()
        )
        != asof
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
    field = {"reference": "p_ref", "marginal": "p_marg", "direct": "p_direct"}[model]
    candidates = []
    for row in rows:
        edge = row[field] * row["odds"] - 1
        if row["valid_log"] and row[field] > row["v_target"] and edge > 0:
            candidates.append((edge, row))
    if not candidates:
        return None
    best = max(edge for edge, _ in candidates)
    return min((row for edge, row in candidates if best - edge <= tolerance), key=lambda r: r["selection"])


def decide(store, race_id, schedule, config, clock=None, analyzer=analyze):
    """Persist exactly once per race/experiment/model; all models share one input cohort."""
    clock = clock or store.clock
    if config["mode"] not in {"EXPLORATORY_SHADOW", "FROZEN_PAPER"} or config["stake_yen"] != 100:
        raise ValueError("PAPER_CONFIG")
    if config["max_tickets_per_race"] != 1 or config["target"] in config["references"]:
        raise ValueError("PAPER_CONFIG")
    experiment = config["version"]
    config_hash = identity(config)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO experiments VALUES(?,?)", (experiment, config_hash))
        registered = store.db.execute(
            "SELECT config_hash FROM experiments WHERE id=?", (experiment,)
        ).fetchone()[0]
        if registered != config_hash:
            raise ValueError("EXPERIMENT_CONFIG_CHANGED")
    existing = store.db.execute(
        "SELECT body FROM decisions WHERE experiment=? AND race_id=? ORDER BY model", (experiment, race_id)
    ).fetchall()
    if existing:
        results = [json.loads(x[0]) for x in existing]
        if any(x["config_hash"] != config_hash for x in results):
            raise ValueError("EXPERIMENT_CONFIG_CHANGED")
        return results
    asof = stamp(
        (
            instant(schedule["scheduled_start_at"]) - timedelta(seconds=config["asof_before_start_seconds"])
        ).isoformat()
    )
    ready_at = stamp(clock())
    if seconds(ready_at, asof) < 0:
        # Early cron calls do not consume the immutable due-time decision.
        return [{"race_id": race_id, "experiment": experiment, "status": "NOT_DUE", "asof_at": asof}]
    view = store.asof(race_id, [config["target"], *config["references"]], asof, config["max_age_seconds"])
    now = ready_at
    reason = eligibility(view, config, schedule, now)
    result = None
    if reason is None:
        content = {h: x["content"] for h, x in view["markets"].items()}
        try:
            result = analyzer(content[config["target"]]["state"]["runners"], content, config)
            if result["identification"]["status"] == "INCONSISTENT":
                reason = "MODEL_ERROR"
        except ModelError as exc:
            reason = (
                "DATA_MISSING"
                if str(exc) in {"INCOMPLETE_MARKET", "UNUSABLE_ODDS", "REFERENCE_MISSING"}
                else "MODEL_ERROR"
            )
    completed = stamp(clock())
    # Recheck deadline after computation, never present start time as completion.
    after_reason = eligibility(view, config, schedule, completed)
    reason = after_reason or reason
    day = (
        instant(schedule["scheduled_start_at"])
        .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Tokyo"))
        .date()
        .isoformat()
    )
    decisions = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        concurrent = store.db.execute(
            "SELECT body FROM decisions WHERE experiment=? AND race_id=? ORDER BY model",
            (experiment, race_id),
        ).fetchall()
        if concurrent:
            return [json.loads(row[0]) for row in concurrent]
        for model in config["comparison_models"]:
            row = (
                select(result["rows"], model, config["tie_tolerance"]) if result and reason is None else None
            )
            model_reason = reason or (None if row else "NO_EDGE")
            used = store.db.execute(
                "SELECT coalesce(sum(stake),0) FROM decisions WHERE experiment=? AND model=? AND day=?",
                (experiment, model, day),
            ).fetchone()[0]
            if row and used + config["stake_yen"] > config["daily_stake_limit_yen_per_model"]:
                row, model_reason = None, "DAILY_LIMIT"
            decision_id = identity([experiment, model, race_id])
            record = {
                "id": decision_id,
                "experiment": experiment,
                "model": model,
                "race_id": race_id,
                "mode": config["mode"],
                "config_hash": config_hash,
                "target": config["target"],
                "status": "PAPER_BET" if row else "NO_BET",
                "reason": model_reason,
                "selection": row["selection"] if row else None,
                "stake_yen": config["stake_yen"] if row else 0,
                "asof_at": asof,
                "decision_at": completed,
                "schedule": schedule,
                "paper_timing_assumption": config["paper_timing_assumption"],
                "sync_evidence": "common_file"
                if view["markets"] and len({x["observation_id"] for x in view["markets"].values()}) == 1
                else "market_times",
                "freshness_basis": config["freshness_basis"],
                "input_view": view,
                "diagnostics": result,
                "code_version": "0.1.0",
                "real_stake_yen": 0,
            }
            store.db.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                (
                    decision_id,
                    experiment,
                    model,
                    race_id,
                    day,
                    record["stake_yen"],
                    canonical(record).decode(),
                ),
            )
            decisions.append(record)
    return decisions


def settle(store, decision_id, payout):
    """Official ticket payout/refund records; no inference from finish order or quote odds."""
    decision = json.loads(
        store.db.execute("SELECT body FROM decisions WHERE id=?", (decision_id,)).fetchone()[0]
    )
    if payout["race_id"] != decision["race_id"] or payout["source_kind"] not in {"SYNTHETIC", "OFFICIAL"}:
        raise ValueError("PAYOUT_SOURCE")
    when = stamp(payout["available_at"])
    if when < decision["decision_at"] or stamp(store.clock()) < when:
        raise ValueError("PAYOUT_TIME")
    digest = identity(payout)
    old = store.db.execute(
        "SELECT body FROM settlements WHERE decision_id=? AND revision=?", (decision_id, payout["revision"])
    ).fetchone()
    if old:
        previous = json.loads(old[0])
        if previous["source_hash"] != digest:
            raise ValueError("PAYOUT_REVISION_CONFLICT")
        return previous
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
    stake = decision["stake_yen"]
    paid = refund = 0
    status = "NO_BET" if stake == 0 else "PENDING"
    if stake and payout.get("final") and decision["target"] in payout.get("complete_markets", []):
        if payout.get("void"):
            refund = stake
        else:
            for r in rows:
                if (r["market"], r["selection"]) == (decision["target"], decision["selection"]):
                    paid = r.get("payout_per_100", 0) * stake / 100
                    refund = r.get("refund_per_100", 0) * stake / 100
        status = "SETTLED"
    record = {
        "decision_id": decision_id,
        "revision": payout["revision"],
        "source_hash": digest,
        "source_kind": payout["source_kind"],
        "source_reference": payout["source_reference"],
        "available_at": when,
        "settled_at": stamp(store.clock()),
        "status": status,
        "stake_yen": stake,
        "payout_yen": paid if status != "PENDING" else None,
        "refund_yen": refund if status != "PENDING" else None,
        "profit_yen": paid + refund - stake if status != "PENDING" else None,
        "roi": (paid + refund - stake) / stake if stake and status == "SETTLED" else None,
    }
    with store.db:
        store.db.execute(
            "INSERT INTO settlements VALUES(?,?,?,?,?)",
            (
                identity([decision_id, payout["revision"]]),
                decision_id,
                payout["revision"],
                when,
                canonical(record).decode(),
            ),
        )
    return record
