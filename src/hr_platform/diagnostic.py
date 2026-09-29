"""One-file static market experiment. Never a historical decision or a Paper bet."""

import time
from math import isfinite
from .common import identity, stamp
from .model import analyze, reference, states, matrix, key, ModelError
from .race_files import normalize
from .realdata import RealData

VERSION = "static-market-diagnostic-v3"


def diagnose(store, odds_raw, odds_name, race_raw, race_name, kind, encoding, config, race_id=None):
    if kind != "DAILY_SNAPSHOT":
        raise ValueError("FINAL_ONLY_NOT_STRATEGY_INPUT")
    adapter = RealData(store)
    odds = adapter.inspect(odds_raw, odds_name, kind, encoding)
    races = adapter.inspect(race_raw, race_name, kind, encoding)
    report = {
        "version": VERSION,
        "input_inspections": [odds["id"], races["id"]],
        "raw_hashes": [odds["raw_hash"], races["raw_hash"]],
        "config": config,
        "config_hash": identity(config),
        "dataset_kind": kind,
        "collector_received_at": None,
        "source_updated_at": None,
        "asof_at": None,
        "paper_eligible": False,
        "decision_input_eligible": False,
        "decision_input_reasons": ["ACQUISITION_TIME_UNKNOWN", "RACE_STATE_UNVERIFIED"],
        "profitability_validated": False,
        "interpretation": "STATIC_MARKET_COMPARISON_NOT_BACKTEST",
        "runner_basis": "LISTED_ENTRIES_NOT_VERIFIED_ACTIVE",
        "market_synchronization": "UNVERIFIED",
    }
    if (
        odds.get("content_type") != "odds"
        or races.get("content_type") != "race"
        or any(r["status"] != "PARSED_UNQUALIFIED" for r in (odds, races))
    ):
        return {**report, "status": "QUARANTINED", "reason": "INPUT_SCHEMA"}
    if config["target"] != "quinella" or config["target"] in config["references"]:
        raise ValueError("FIRST_EXPERIMENT_QUINELLA_REQUIRED")
    candidates = sorted(set(odds["content"]) & set(races["content"]["races"]))
    if race_id is not None:
        candidates = [race_id] if race_id in candidates else []
    excluded = []
    selected = None
    for candidate in candidates:
        metadata = races["content"]["races"][candidate]
        # Local static research can use completed races, but never labels them pre-race.
        if "帯広" in candidate or normalize(metadata["surface_label"]) not in {"芝", "ダート"}:
            excluded.append({"race_id": candidate, "reason": "NOT_CONFIRMED_FLAT"})
            continue
        runners = sorted(map(int, metadata["horses"]))
        if not metadata["entry_count_matches"]:
            excluded.append({"race_id": candidate, "reason": "ENTRY_COUNT_MISMATCH"})
            continue
        markets = odds["content"][candidate]["markets"]
        try:
            omega = states(runners)
        except ModelError:
            excluded.append({"race_id": candidate, "reason": "RUNNER_COUNT"})
            continue
        issues = []
        for market in [config["target"], *config["references"]]:
            expected = {key(selection) for selection in matrix(omega, market)[0]}
            quotes = markets.get(market, {}).get("quotes", {})
            try:
                if market not in markets:
                    raise ModelError("MARKET_MISSING")
                reference(omega, market, quotes)
            except ModelError as exc:
                issues.append({
                    "market": market, "reason": str(exc),
                    "expected_rows": len(expected), "observed_rows": len(quotes),
                    "missing_selections": sorted(expected - quotes.keys()),
                    "unexpected_selections": sorted(quotes.keys() - expected),
                    "unusable_selections": [
                        {"selection": selection, "display_status": quote.get("display_status"),
                         "raw_odds": quote.get("raw_odds"), "raw_max": quote.get("raw_max")}
                        for selection, quote in quotes.items()
                        if quote.get("display_status") != "FIXED" or quote.get("odds") is None
                        or not isfinite(quote["odds"]) or quote["odds"] < 1
                    ],
                    "marker_meaning": "NOT_INFERRED_FROM_DISPLAY_VALUE",
                })
        if issues:
            excluded.append({"race_id": candidate, "reason": "MARKET_QUALITY", "issues": issues})
            continue
        selected = candidate, metadata, runners, markets
        break  # deterministic first eligible race, never selected by price differences or results
    report["excluded_before_selection"] = excluded
    if selected is None:
        return {**report, "status": "NO_COMPATIBLE_RACE"}
    candidate, metadata, runners, markets = selected
    report.update(race_id=candidate, result_fields_present=metadata["result_present"])
    if metadata["result_present"]:
        report["decision_input_reasons"].append("RESULTS_ALREADY_PRESENT")
    started = time.perf_counter()
    try:
        report.update(status="STATIC_DIAGNOSTIC", analysis=analyze(runners, markets, config))
        if report["analysis"]["identification"]["status"] == "INCONSISTENT":
            report.update(status="MODEL_ERROR", reason="REFERENCE_CONSTRAINTS_INCONSISTENT")
    except ModelError:
        report.update(status="MODEL_ERROR", reason="MODEL_DID_NOT_PRODUCE_VALID_DISTRIBUTION")
    report.update(duration_ms=(time.perf_counter() - started) * 1000, analyzed_at=stamp(store.clock()))
    return report
