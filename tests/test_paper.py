import copy
import json
import pytest
from hr_platform import fixtures as f
from hr_platform.model import ModelError
from hr_platform.paper import decide, settle, eligibility
from hr_platform.demo import run


def test_demo_single_command_reopen_and_repeat(tmp_path):
    a = run(tmp_path)
    b = run(tmp_path)
    assert a["metrics"]["observations"] == b["metrics"]["observations"] == 4
    assert a["asof_observation"] == "a-again"
    main = next(d for d in a["decisions"] if d["model"] == "reference")
    assert main["selection"] == "1-2" and main["stake_yen"] == 100
    assert len(a["settlements"]) == 3


def test_fixed_decisions_replay_schedule_change(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    later = f.schedule()
    later.update(version="delayed-v2", scheduled_start_at=f.at(30))
    collected.clock = lambda: f.at(20)
    again = decide(collected, f.RACE, later, config)
    assert {d["id"] for d in decisions} == {d["id"] for d in again}
    assert collected.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 3
    changed = copy.deepcopy(config)
    changed["lambda"] = 0.01
    with pytest.raises(ValueError, match="EXPERIMENT_CONFIG_CHANGED"):
        decide(collected, f.RACE, later, changed)


@pytest.mark.parametrize(
    "reason",
    ["STALE", "DATA_MISSING", "RACE_STATE_CHANGED", "OUT_OF_SCOPE", "RACE_NOT_PRE_RACE", "ASYNCHRONOUS"],
)
def test_quality_gates(collected, config, reason):
    view = collected.asof(f.RACE, ["quinella", "win", "exacta"], f.at(4))
    if reason in {"STALE", "DATA_MISSING"}:
        view["reason"] = reason
    elif reason == "RACE_STATE_CHANGED":
        view["markets"]["exacta"]["state_hash"] = "changed"
    elif reason == "OUT_OF_SCOPE":
        view["markets"]["exacta"]["content"]["state"]["discipline"] = "BANEI"
    elif reason == "RACE_NOT_PRE_RACE":
        view["markets"]["exacta"]["content"]["state"]["status"] = "FINAL"
    else:
        view["markets"]["exacta"]["observation_id"] = "different"
    assert eligibility(view, config, f.schedule(), f.at(4, 20)) == reason


def test_turf_and_late_schedule_known(collected, config):
    view = collected.asof(f.RACE, ["quinella", "win", "exacta"], f.at(4))
    for m in view["markets"].values():
        m["content"]["state"]["surface"] = "TURF"
    assert eligibility(view, config, f.schedule(), f.at(4, 20)) is None
    schedule = f.schedule()
    schedule["known_at"] = f.at(5)
    assert eligibility(view, config, schedule, f.at(4, 20)) == "SCHEDULE_NOT_KNOWN"


def test_model_failure_keeps_collection(collected, config):
    def broken(*_):
        raise ModelError("FAILURE")

    records = decide(collected, f.RACE, f.schedule(), config, analyzer=broken)
    assert all(d["reason"] == "MODEL_ERROR" for d in records)
    collected.clock = lambda: f.at(6, 2)
    collected.ingest(f.event("next", 6), f.archive("nonuniform"))
    assert collected.metrics()["observations"] == 2


def test_late_completion(collected, config):
    moments = iter([f.at(4, 10), f.at(7)])
    records = decide(collected, f.RACE, f.schedule(), config, clock=lambda: next(moments))
    assert all(d["reason"] == "DECISION_TOO_LATE" for d in records)


def test_settlement_pending_dead_heat_refund_void_corrections(collected, config):
    d = next(x for x in decide(collected, f.RACE, f.schedule(), config) if x["model"] == "reference")
    before = json.loads(
        collected.db.execute("SELECT body FROM decisions WHERE id=?", (d["id"],)).fetchone()[0]
    )
    collected.clock = lambda: f.at(21)
    pending = settle(collected, d["id"], f.payout("pending", final=False))
    assert pending["status"] == "PENDING" and pending["profit_yen"] is None
    pay = f.payout("dead-heat")
    pay["tickets"].append({"market": "quinella", "selection": "1-3", "payout_per_100": 350})
    settled = settle(collected, d["id"], pay)
    assert settled["profit_yen"] == 550 and settled["roi"] == 5.5
    assert settle(collected, d["id"], pay) == settled
    refunded = settle(
        collected,
        d["id"],
        f.payout("refund", tickets=[{"market": "quinella", "selection": "1-2", "refund_per_100": 100}]),
    )
    assert refunded["profit_yen"] == 0
    void = settle(collected, d["id"], f.payout("void", void=True))
    assert void["refund_yen"] == 100
    corrected = settle(collected, d["id"], f.payout("correction", tickets=[]))
    assert corrected["profit_yen"] == -100
    assert (
        json.loads(collected.db.execute("SELECT body FROM decisions WHERE id=?", (d["id"],)).fetchone()[0])
        == before
    )
    assert (
        collected.db.execute("SELECT count(*) FROM settlements WHERE decision_id=?", (d["id"],)).fetchone()[0]
        == 5
    )
    with pytest.raises(ValueError, match="PAYOUT_REVISION_CONFLICT"):
        settle(collected, d["id"], f.payout("correction"))


def test_no_bet_roi_undefined(collected, config):
    config["daily_stake_limit_yen_per_model"] = 0
    decisions = decide(collected, f.RACE, f.schedule(), config)
    assert all(x["stake_yen"] == 0 for x in decisions)
    collected.clock = lambda: f.at(21)
    assert all(settle(collected, x["id"], f.payout())["roi"] is None for x in decisions)


def test_experiment_config_is_fixed_across_races(collected, config):
    decide(collected, f.RACE, f.schedule(), config)
    config["lambda"] = 0.01
    with pytest.raises(ValueError, match="EXPERIMENT_CONFIG_CHANGED"):
        decide(collected, "a-different-race", f.schedule(), config)


def test_early_call_does_not_consume_due_time_decision(collected, config):
    collected.clock = lambda: f.at(2, 2)
    assert decide(collected, f.RACE, f.schedule(), config)[0]["status"] == "NOT_DUE"
    assert collected.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    collected.clock = lambda: f.at(4, 20)
    assert len(decide(collected, f.RACE, f.schedule(), config)) == 3


@pytest.mark.parametrize("amount", [70, 80])
def test_special_payout_replay_and_refund_priority(collected, config, amount):
    d = next(x for x in decide(collected, f.RACE, f.schedule(), config) if x["model"] == "reference")
    collected.clock = lambda: f.at(21)
    special = [{"market": "quinella", "payout_per_100": amount}]
    pay = f.payout("special", tickets=[], special_payouts=special)
    result = settle(collected, d["id"], pay)
    assert result["payout_yen"] == result["special_payout_yen"] == amount
    assert result["refund_yen"] == 0 and result["profit_yen"] == amount - 100
    assert settle(collected, d["id"], pay) == result
    assert json.loads(collected.read_body(result["source_hash"], "receipts")) == pay
    refunded = settle(collected, d["id"], f.payout(
        "special-with-refund", special_payouts=special,
        tickets=[{"market": "quinella", "selection": d["selection"], "refund_per_100": 100}],
    ))
    assert refunded["refund_yen"] == 100
    assert refunded["special_payout_yen"] == refunded["payout_yen"] == 0
    assert refunded["profit_yen"] == 0


@pytest.mark.parametrize("overrides", [
    {"final": False}, {"complete_markets": []},
])
def test_special_payout_stays_pending_without_completeness(collected, config, overrides):
    d = next(x for x in decide(collected, f.RACE, f.schedule(), config) if x["model"] == "reference")
    collected.clock = lambda: f.at(21)
    pay = f.payout(tickets=[], special_payouts=[{"market": "quinella", "payout_per_100": 70}], **overrides)
    result = settle(collected, d["id"], pay)
    assert result["status"] == "PENDING"
    assert result["payout_yen"] is result["special_payout_yen"] is result["profit_yen"] is None


@pytest.mark.parametrize("overrides,error", [
    ({"special_payouts": [{"market": "quinella", "payout_per_100": 0}]}, "AMOUNT"),
    ({"special_payouts": [{"market": "unknown", "payout_per_100": 70}]}, "MARKET"),
    ({"special_payouts": [{"market": "quinella", "payout_per_100": 70}] * 2}, "MARKET"),
    ({"void": True}, "CONFLICT"),
    ({"tickets": [{"market": "quinella", "selection": "1-2", "payout_per_100": 100}]}, "CONFLICT"),
    ({"tickets": [{"market": "quinella", "selection": "1-2", "refund_per_100": 50}]}, "CONFLICT"),
])
def test_special_payout_invalid_inputs_do_not_settle(collected, config, overrides, error):
    d = decide(collected, f.RACE, f.schedule(), config)[0]
    collected.clock = lambda: f.at(21)
    pay = f.payout(tickets=[], special_payouts=[{"market": "quinella", "payout_per_100": 70}])
    pay.update(overrides)
    with pytest.raises(ValueError, match="SPECIAL_PAYOUT_" + error):
        settle(collected, d["id"], pay)
    assert collected.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_special_payout_does_not_apply_to_other_market(collected, config):
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        result = settle(collected, decision["id"], f.payout(
            tickets=[], special_payouts=[{"market": "exacta", "payout_per_100": 80}],
        ))
        assert result["payout_yen"] == result["special_payout_yen"] == 0
        assert result["profit_yen"] == -decision["stake_yen"]


def test_special_payout_never_pays_no_bet(collected, config):
    config["daily_stake_limit_yen_per_model"] = 0
    decisions = decide(collected, f.RACE, f.schedule(), config)
    collected.clock = lambda: f.at(21)
    for decision in decisions:
        result = settle(collected, decision["id"], f.payout(
            tickets=[], special_payouts=[{"market": "quinella", "payout_per_100": 70}],
        ))
        assert result["status"] == "NO_BET"
        assert result["payout_yen"] == result["special_payout_yen"] == result["profit_yen"] == 0
        assert result["roi"] is None
