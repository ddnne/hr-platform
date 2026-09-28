import pytest
import numpy as np
from hr_platform import fixtures as f
from hr_platform.diagnostic import diagnose
from test_realdata import race_archive


def run(store, config, odds=None, race=None, kind="DAILY_SNAPSHOT"):
    return diagnose(
        store,
        odds or f.archive("nonuniform"),
        "odds.zip",
        race or race_archive(),
        "race.zip",
        kind,
        "utf-8-sig",
        config,
    )


def test_static_diagnosis_compares_three_models_without_backdating_or_bets(store, config):
    report = run(store, config)
    assert report["status"] == "STATIC_DIAGNOSTIC"
    assert report["race_id"] == f.RACE
    assert report["collector_received_at"] is report["asof_at"] is None
    assert not report["paper_eligible"] and not report["profitability_validated"]
    assert report["market_synchronization"] == "UNVERIFIED"
    row = report["analysis"]["rows"][0]
    assert all(k in row for k in ("p_ref", "p_direct", "p_marg", "d_price", "d_dep", "d_rest"))
    assert row["d_price"] == pytest.approx(row["d_dep"] + row["d_rest"])
    assert store.metrics()["observations"] == 0
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_results_remain_static_and_do_not_change_fitted_distribution(store, config):
    before = run(store, config)
    after = run(store, config, race=race_archive(finished=True))
    assert after["result_fields_present"]
    assert "RESULTS_ALREADY_PRESENT" in after["decision_input_reasons"]
    assert not after["decision_input_eligible"]
    np.testing.assert_allclose(before["analysis"]["q_ref"], after["analysis"]["q_ref"])
    assert store.asof(f.RACE, ["win"], f.at(30))["reason"] == "DATA_MISSING"


def test_final_only_never_reaches_strategy_diagnosis(store, config):
    with pytest.raises(ValueError, match="FINAL_ONLY_NOT_STRATEGY_INPUT"):
        run(store, config, kind="FINAL_ONLY")
    assert store.metrics()["observations"] == 0


def test_incomplete_entries_are_not_silently_dropped(store, config):
    report = run(store, config, race=race_archive(horse_count=3))
    assert report["status"] == "NO_COMPATIBLE_RACE"
    assert report["excluded_before_selection"][0]["reason"] == "ENTRY_COUNT_MISMATCH"


def test_target_cannot_be_a_reference(store, config):
    with pytest.raises(ValueError, match="FIRST_EXPERIMENT"):
        run(store, {**config, "references": ["win", "quinella"]})


def test_realfile_shape_failure_is_not_reported_as_strategy_failure(store, config):
    report = run(store, config, odds=b"invalid ZIP")
    assert report["status"] == "QUARANTINED"
    assert store.metrics()["raw_objects"] == 2
