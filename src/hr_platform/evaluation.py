"""Private ledger comparison. Missing outcomes never become zero payouts."""

from collections import Counter
from itertools import combinations
import json
import math
from .common import MODEL_PROBABILITY_FIELDS, identity, stamp


SCORE_FIELDS = {**MODEL_PROBABILITY_FIELDS, "market": "v_target"}


def purchase_counts(entries):
    """Count distinct purchased combinations; refunds are not hits."""
    bets = [entry for entry in entries if entry['stake_yen'] > 0]
    pending = any(entry['payout_yen'] is None for entry in bets)
    hits = [entry for entry in bets if entry['payout_yen'] is not None
            and entry['payout_yen'] - entry.get('special_payout_yen', 0) > 0]
    def ticket_key(entry):
        return entry['race_id'], entry['target'], entry['selection']
    return {
        'bet_race_count': len({entry['race_id'] for entry in bets}),
        'purchased_ticket_count': len({ticket_key(entry) for entry in bets}),
        'hit_race_count': None if pending else len({entry['race_id'] for entry in hits}),
        'hit_ticket_count': None if pending else len({ticket_key(entry) for entry in hits}),
    }


def _score_race(store, race_id, decisions, entries):
    """Score a whole frozen outcome distribution, never only the purchased ticket."""
    diagnostics = [d.get("diagnostics") for d in decisions]
    if not all(isinstance(d, dict) and d for d in diagnostics) or len({identity(d) for d in diagnostics}) != 1:
        return {"status": "PREDICTIONS_UNAVAILABLE_OR_DIFFERENT"}
    decision = decisions[0]
    target = decision["target"]
    if target not in {"quinella", "trio"}:
        return {"status": "UNSUPPORTED_TARGET"}
    horses = decision["input_view"]["markets"][target]["content"]["state"]["runners"]
    support = {"-".join(map(str, x)) for x in combinations(sorted(horses), 2 if target == "quinella" else 3)}
    rows = diagnostics[0].get("rows", [])
    if (not support or not isinstance(rows, list) or len(rows) != len(support)
            or any(not isinstance(r, dict) or not isinstance(r.get("selection"), str) for r in rows)
            or {r["selection"] for r in rows} != support):
        return {"status": "PREDICTION_SUPPORT_INVALID"}
    for field in SCORE_FIELDS.values():
        values = [r.get(field) for r in rows]
        if (any(type(p) not in {int, float} or not math.isfinite(p) or not 0 <= p <= 1 for p in values)
                or abs(sum(values) - 1) > 1e-7):
            return {"status": "PREDICTION_PROBABILITIES_INVALID"}
    settlements = [e["settlement"] for e in entries]
    if (not all(settlements) or any(e["status"] == "AMBIGUOUS_REVISION" for e in entries)):
        return {"status": "PAYOUT_UNAVAILABLE"}
    if len({s["source_hash"] for s in settlements}) != 1:
        return {"status": "PAYOUT_EVIDENCE_DIFFERS"}
    payout = json.loads(store.read_body(settlements[0]["source_hash"], "receipts"))
    if (payout["race_id"] != race_id or not payout.get("final")
            or target not in payout.get("complete_markets", [])
            or payout.get("source_kind") not in {"SYNTHETIC", "OFFICIAL"}):
        return {"status": "PAYOUT_UNQUALIFIED"}
    tickets = [t for t in payout.get("tickets", []) if t["market"] == target]
    if (payout.get("void") or payout.get("special_payouts")
            or any(t.get("refund_per_100", 0) > 0 for t in tickets)
            or any(r["status"] in {"EXCLUDED", "CANCELLED_BEFORE_SALES"}
                   for r in payout.get("runners", {}).values())):
        return {"status": "NON_ORDINARY_OUTCOME"}
    winners = [t["selection"] for t in tickets if t.get("payout_per_100", 0) > 0]
    if len(winners) != 1 or winners[0] not in support:
        return {"status": "SINGLE_OUTCOME_UNAVAILABLE"}
    winner = winners[0]
    scores = {}
    for model, field in SCORE_FIELDS.items():
        probability = next(r[field] for r in rows if r["selection"] == winner)
        scores[model] = {
            "outcome_probability": probability,
            "brier": sum((r[field] - int(r["selection"] == winner)) ** 2 for r in rows),
            "log_loss": -math.log(probability) if probability else None,
            "log_loss_status": "FINITE" if probability else "INFINITE",
        }
    return {"status": "SCORED", "outcome_selection": winner, "models": scores,
            "payout_revision": settlements[0]["revision"], "source_kind": payout["source_kind"]}


def _prediction_scores(store, races, summaries):
    by_model = {m: {e["race_id"]: e for e in s["entries"]} for m, s in summaries.items()}
    entries = [{"race_id": race, **_score_race(store, race, list(records.values()),
                                             [by_model[m][race] for m in records])}
               for race, records in sorted(races.items())]
    scored = [e for e in entries if e["status"] == "SCORED"]
    means = {}
    for model in SCORE_FIELDS:
        rows = [e["models"][model] for e in scored]
        zeros = sum(r["log_loss_status"] == "INFINITE" for r in rows)
        means[model] = {
            "race_count": len(rows), "zero_outcome_probability_count": zeros,
            "mean_brier": sum(r["brier"] for r in rows) / len(rows) if rows else None,
            "mean_log_loss": sum(r["log_loss"] for r in rows) / len(rows) if rows and not zeros else None,
            "log_loss_status": "INFINITE" if zeros else "FINITE" if rows else "UNAVAILABLE",
        }
    return {"version": "paper-probability-score-v1", "basis": "RECORDED_MARKET_IMPLIED_Q_AND_ASOF_SETTLEMENT",
            "unit": "ONE_RACE_ONE_ORDINARY_OUTCOME", "brier_definition": "SUM_OVER_ALL_TICKETS",
            "log_base": "e", "cohort": [e["race_id"] for e in scored], "scored_race_count": len(scored),
            "unscored_reason_counts": dict(Counter(e["status"] for e in entries if e["status"] != "SCORED")),
            "models": means, "entries": entries}


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
        "issues": issues, "models": {}, "prediction_scores": None, "profitability_verified": False,
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
        reference_statuses = Counter()
        assumptions = Counter()
        for decision in decisions:
            reasons[decision["reason"] or "PAPER_BET"] += 1
            identification = (decision.get("diagnostics") or {}).get("identification", {})
            reference_status = identification.get("status", "NOT_COMPUTED")
            reference_statuses[reference_status] += 1
            assumptions.update(decision.get("research_assumptions", []))
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
                "target": decision["target"], "selection": decision["selection"],
                "asof_at": decision["asof_at"], "status": status, "stake_yen": stake,
                "reference_constraint_status": reference_status,
                "research_assumptions": decision.get("research_assumptions", []),
                "settlement": latest,
                "payout_yen": latest["payout_yen"] if settled else 0 if not stake else None,
                "special_payout_yen": latest.get("special_payout_yen", 0) if settled else 0 if not stake else None,
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
            **purchase_counts(entries),
            "no_bet_count": no_bet, "skip_rate": no_bet / len(entries) if entries else None,
            "reason_counts": dict(reasons), "input_kind_counts": dict(input_kinds),
            "reference_constraint_status_counts": dict(reference_statuses),
            "research_assumption_counts": dict(assumptions),
            "settlement_source_counts": dict(source_kinds), "pending_count": len(pending),
            "complete": not pending and bool(entries), "stake_yen": stake,
            "payout_yen": paid, "refund_yen": refunds, "profit_yen": profit,
            "roi": profit / stake if stake and not pending else None,
            "max_drawdown_yen_by_decision_time": drawdown if entries and not pending else None,
            "entries": entries,
        }
    report["prediction_scores"] = _prediction_scores(store, races, report["models"])
    return report
