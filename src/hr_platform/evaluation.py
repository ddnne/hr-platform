"""Private ledger comparison. Missing outcomes never become zero payouts."""

from collections import Counter
import json
from .common import identity, stamp


def compare(store, config, at):
    cutoff = stamp(at)
    models = config["comparison_models"]
    if len(set(models)) != len(models) or set(models) != {"direct", "marginal", "reference"}:
        raise ValueError("COMPARISON_MODELS")
    config_hash = identity(config)
    registered = store.db.execute(
        "SELECT config_hash FROM experiments WHERE id=?", (config["version"],)
    ).fetchone()
    if registered and registered[0] != config_hash:
        raise ValueError("EXPERIMENT_CONFIG_CHANGED")
    races = {}
    for row in store.db.execute("SELECT body FROM decisions WHERE experiment=?", (config["version"],)):
        decision = json.loads(row[0])
        if stamp(decision["decision_at"]) > cutoff:
            continue
        if decision["config_hash"] != config_hash or decision["model"] not in models:
            raise ValueError("DECISION_CONFIG_MISMATCH")
        races.setdefault(decision["race_id"], {})[decision["model"]] = decision
    issues = []
    for race, records in races.items():
        if set(records) != set(models):
            issues.append({"race_id": race, "reason": "MISSING_MODEL_DECISION"})
        elif len({identity([d["asof_at"], d["input_view"], d["schedule"]])
                  for d in records.values()}) != 1:
            issues.append({"race_id": race, "reason": "DIFFERENT_INPUTS"})
    report = {
        "experiment": config["version"], "config_hash": config_hash, "asof_at": cutoff,
        "generated_at": stamp(store.clock()), "cohort": sorted(races), "race_count": len(races),
        "cohort_basis": "RECORDED_DECISIONS_NOT_ALL_SCHEDULED_RACES",
        "status": "COHORT_MISMATCH" if issues else "EMPTY" if not races else "COMPARED",
        "issues": issues, "models": {}, "profitability_verified": False,
        "settlement_order": "LATEST_LOCAL_RECORD_NOT_PROVIDER_REVISION_ORDER",
    }
    if issues:
        return report
    for model in models:
        decisions = sorted((records[model] for records in races.values()),
                           key=lambda d: (d["asof_at"], d["race_id"]))
        entries = []
        reasons = Counter()
        input_kinds = Counter()
        source_kinds = Counter()
        for decision in decisions:
            reasons[decision["reason"] or "PAPER_BET"] += 1
            kinds = {x["event"]["dataset_kind"] for x in decision["input_view"]["markets"].values()}
            input_kinds.update(kinds or {"NO_INPUT"})
            records = [json.loads(row[0]) for row in store.db.execute(
                "SELECT body FROM settlements WHERE decision_id=?", (decision["id"],))]
            records = [r for r in records if r.get("recorded_at")
                       and stamp(r["recorded_at"]) <= cutoff and stamp(r["available_at"]) <= cutoff
                       and stamp(r["settled_at"]) <= cutoff]
            latest = None
            ambiguous = False
            if records:
                newest = max(stamp(r["recorded_at"]) for r in records)
                candidates = [r for r in records if stamp(r["recorded_at"]) == newest]
                ambiguous = len(candidates) != 1
                if not ambiguous:
                    latest = candidates[0]
                    source_kinds[latest["source_kind"]] += 1
            stake = decision["stake_yen"]
            status = "NO_BET" if not stake else (
                "AMBIGUOUS_REVISION" if ambiguous else latest["status"] if latest else "PENDING")
            settled = status == "SETTLED"
            entries.append({
                "race_id": decision["race_id"], "decision_id": decision["id"],
                "asof_at": decision["asof_at"], "status": status, "stake_yen": stake,
                "settlement": latest,
                "payout_yen": latest["payout_yen"] if settled else 0 if not stake else None,
                "refund_yen": latest["refund_yen"] if settled else 0 if not stake else None,
                "profit_yen": latest["profit_yen"] if settled else 0 if not stake else None,
            })
        pending = [e for e in entries if e["profit_yen"] is None]
        stake = sum(e["stake_yen"] for e in entries)
        paid = sum(e["payout_yen"] for e in entries) if not pending else None
        refunds = sum(e["refund_yen"] for e in entries) if not pending else None
        profit = paid + refunds - stake if not pending else None
        equity = peak = drawdown = 0
        if not pending:
            # Batch simultaneous decision times; race-id tie order is not economic evidence.
            by_time = Counter()
            for entry in entries:
                by_time[stamp(entry["asof_at"])] += entry["profit_yen"]
            for time in sorted(by_time):
                equity += by_time[time]
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
        no_bet = sum(not e["stake_yen"] for e in entries)
        report["models"][model] = {
            "race_count": len(entries), "bet_count": len(entries) - no_bet,
            "no_bet_count": no_bet, "skip_rate": no_bet / len(entries) if entries else None,
            "reason_counts": dict(reasons), "input_kind_counts": dict(input_kinds),
            "settlement_source_counts": dict(source_kinds), "pending_count": len(pending),
            "complete": not pending and bool(entries), "stake_yen": stake,
            "payout_yen": paid, "refund_yen": refunds, "profit_yen": profit,
            "roi": profit / stake if stake and not pending else None,
            "max_drawdown_yen_by_decision_time": drawdown if entries and not pending else None,
            "entries": entries,
        }
    return report
