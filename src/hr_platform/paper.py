import json
from .common import canonical, identity, instant, paper_asof, stamp, seconds
from .model import analyze, ModelError
from .paper_rules import eligibility, select, settlement_values


def register_experiment(store, config):
    experiment, config_hash = config["version"], identity(config)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO experiments VALUES(?,?)", (experiment, config_hash))
        registered = store.db.execute(
            "SELECT config_hash FROM experiments WHERE id=?", (experiment,)
        ).fetchone()[0]
        if registered != config_hash:
            raise ValueError("EXPERIMENT_CONFIG_CHANGED")
    return config_hash


def decide(store, race_id, schedule, config, clock=None, analyzer=analyze):
    """Persist exactly once per race/experiment/model; all models share one input cohort."""
    clock = clock or store.clock
    if "input_policy" in config:
        from .prospective import validate_policy

        validate_policy(config)
    if config["mode"] not in {"EXPLORATORY_SHADOW", "FROZEN_PAPER"} or config["stake_yen"] != 100:
        raise ValueError("PAPER_CONFIG")
    if config["max_tickets_per_race"] != 1 or config["target"] in config["references"]:
        raise ValueError("PAPER_CONFIG")
    reference_policy = config.get("reference_constraint_policy", "require_feasible")
    if reference_policy not in {"require_feasible", "allow_inconsistent_shadow"} or (
        reference_policy == "allow_inconsistent_shadow" and config["mode"] != "EXPLORATORY_SHADOW"
    ):
        raise ValueError("REFERENCE_CONSTRAINT_POLICY")
    experiment = config["version"]
    config_hash = register_experiment(store, config)
    existing = store.db.execute(
        "SELECT body FROM decisions WHERE experiment=? AND race_id=? ORDER BY model", (experiment, race_id)
    ).fetchall()
    if existing:
        results = [json.loads(x[0]) for x in existing]
        if any(x["config_hash"] != config_hash for x in results):
            raise ValueError("EXPERIMENT_CONFIG_CHANGED")
        return results
    asof = paper_asof(schedule, config, race_id)
    ready_at = stamp(clock())
    if seconds(ready_at, asof) < 0:
        # Early cron calls do not consume the immutable due-time decision.
        return [{"race_id": race_id, "experiment": experiment, "status": "NOT_DUE", "asof_at": asof}]
    if "input_policy" in config:
        from .prospective import build_view

        view = build_view(store, race_id, schedule, config, asof)
    else:
        view = store.asof(race_id, [config["target"], *config["references"]], asof, config["max_age_seconds"])
    now = ready_at
    reason = eligibility(view, config, schedule, now, race_id=race_id)
    result = None
    assumptions = list(view.get("research_assumptions", []))
    if reason is None:
        content = {h: x["content"] for h, x in view["markets"].items()}
        try:
            result = analyzer(content[config["target"]]["state"]["runners"], content, config)
            if result["identification"]["status"] == "INCONSISTENT":
                if reference_policy == "require_feasible":
                    reason = "REFERENCE_INCONSISTENT"
                else:
                    assumptions.append("INCONSISTENT_REFERENCES_SOFT_CALIBRATION")
        except ModelError as exc:
            reason = (
                "DATA_MISSING"
                if str(exc) in {"INCOMPLETE_MARKET", "UNUSABLE_ODDS", "REFERENCE_MISSING"}
                else "MODEL_ERROR"
            )
        except Exception:
            # Unexpected solver/model failures consume this fixed decision as a
            # no-bet too. They must not leave it open for a later price retry.
            reason = "MODEL_ERROR"
    model_completed = stamp(clock())
    # Recheck deadline after computation, never present start time as completion.
    after_reason = eligibility(view, config, schedule, model_completed, race_id=race_id)
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
                "decision_started_at": ready_at,
                "model_completed_at": model_completed,
                "decision_timing_basis": "CANDIDATES_FINALIZED_BEFORE_LEDGER_COMMIT",
                "schedule": schedule,
                "paper_timing_assumption": config["paper_timing_assumption"],
                "sync_evidence": "common_file"
                if view["markets"] and len({x["observation_id"] for x in view["markets"].values()}) == 1
                else "market_times",
                "freshness_basis": config["freshness_basis"],
                "reference_constraint_policy": reference_policy,
                "research_assumptions": assumptions,
                "input_view": view,
                "diagnostics": result,
                "code_version": "0.1.0",
                "real_stake_yen": 0,
            }
            decisions.append(record)
        # Lock acquisition, candidate selection and daily-limit checks also take
        # time. All three candidates must be final before the common deadline.
        completed = stamp(clock())
        after_reason = eligibility(view, config, schedule, completed, race_id=race_id)
        for record in decisions:
            record["decision_at"] = completed
            if after_reason:
                record.update(status="NO_BET", reason=after_reason, selection=None, stake_yen=0)
            store.db.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                (
                    record["id"],
                    experiment,
                    record["model"],
                    race_id,
                    day,
                    record["stake_yen"],
                    canonical(record).decode(),
                ),
            )
    # Match the persisted JSON representation on first delivery and replay.
    return json.loads(canonical(decisions))


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
        if not previous.get("recorded_at"):
            store.body(canonical(payout), "receipts")
            previous["recorded_at"] = stamp(store.clock())
            with store.db:
                store.db.execute("UPDATE settlements SET body=? WHERE decision_id=? AND revision=?",
                                 (canonical(previous).decode(), decision_id, payout["revision"]))
        return previous
    values = settlement_values(decision, payout)
    record = {
        "decision_id": decision_id,
        "revision": payout["revision"],
        "source_hash": digest,
        "source_kind": payout["source_kind"],
        "source_reference": payout["source_reference"],
        "available_at": when,
        "settled_at": stamp(store.clock()),
        **values,
        "recorded_at": None,
    }
    # Retain the exact settlement input behind source_hash, not only its digest.
    store.body(canonical(payout), "receipts")
    record["settled_at"] = stamp(store.clock())
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
    # A committed row without this publication marker remains unavailable to queries.
    # Interrupted publication can be repaired by replay, using the replay's actual time.
    record["recorded_at"] = stamp(store.clock())
    with store.db:
        store.db.execute("UPDATE settlements SET body=? WHERE decision_id=? AND revision=?",
                         (canonical(record).decode(), decision_id, payout["revision"]))
    return record
