import json
from copy import deepcopy
import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical, stamp
from hr_platform.prospective import enroll
from hr_platform.sampling import Samples
from hr_platform.session import step
from hr_platform.store import Store
from test_prospective import metadata
from test_realdata import race_archive
from test_race_state import page
from test_official_payout import page as payout_page
from test_sampling import plan as capture_plan, Response


@pytest.fixture
def session(store, config, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    metadata(store, -10)
    paper = enroll(store, f.RACE, config)
    capture = capture_plan(("odds", "race", "state", "odds", "payout"), (-3, -1, 1, 3, 24))
    calls = []
    def opener(item):
        calls.append(item["id"])
        kind = item["kind"]
        raw = {"odds": f.archive(), "race": race_archive(),
               "state": page("14:01現在", "").replace(b"14:10", b"14:14"),
               "payout": payout_page()}[kind]
        return Response(raw, kind=kind)
    samples = Samples(store, opener)
    first = step(store, paper["id"], capture, samples=samples)
    assert first["status"] == "WAITING" and first["next_at"] == stamp(f.at(-3)) and calls == []
    return store, paper, capture, samples, calls


def run_at(session, minute, second=10):
    store, paper, capture, samples, _ = session
    store.clock = lambda: f.at(minute, second)
    return step(store, paper["id"], capture, samples=samples)


def test_real_parser_pipeline_through_fixed_decision_and_official_refund(session):
    store, paper, capture, samples, calls = session
    for minute in [-3, -1, 1, 3]:
        result = run_at(session, minute)
        assert result["status"] == "WAITING"
        assert result["decisions"][0]["status"] == "NOT_DUE"
        assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    due = run_at(session, 4, 20)
    assert len(due["decisions"]) == 3 and all(d["status"] == "PAPER_BET" for d in due["decisions"])
    assert due["settlements"] == [] and due["next_at"] == stamp(f.at(24))
    before = store.asof(f.RACE, ["quinella", "win", "exacta"], f.at(4))
    assert len({canonical(d["input_view"]) for d in due["decisions"]}) == 1
    result = run_at(session, 24)
    assert result["status"] == "COMPLETE" and result["next_at"] is None
    assert all(r["refund_yen"] == 100 and r["profit_yen"] == 0 for r in result["settlements"])
    assert result["decisions"] == due["decisions"]
    assert store.asof(f.RACE, ["quinella", "win", "exacta"], f.at(4)) == before
    assert calls == [f"sample-{i}" for i in range(5)]
    assert run_at(session, 30) == result
    reopened = Store(store.root, clock=lambda: f.at(31))
    try:
        no_http = Samples(reopened, lambda _: pytest.fail("replay HTTP"))
        assert step(reopened, paper["id"], capture, samples=no_http) == result
    finally:
        reopened.close()
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 3
    assert store.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 3


def test_missed_pre_cutoff_slots_never_fetch_late_and_stay_no_bet(session):
    _, _, _, _, calls = session
    due = run_at(session, 7)
    assert calls == [] and len(due["decisions"]) == 3
    assert all(d["status"] == "NO_BET" for d in due["decisions"])
    later = run_at(session, 24)
    assert later["status"] == "COMPLETE" and calls == ["sample-4"]
    assert later["decisions"] == due["decisions"]
    assert all(x["status"] == "NO_BET" for x in later["settlements"])


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
def test_model_failure_does_not_disable_later_capture_or_forge_loss(session, monkeypatch, error):
    store, _, _, samples, calls = session
    for minute in [-3, -1, 1, 3]:
        run_at(session, minute)
    original = __import__("hr_platform.session", fromlist=["tick"]).tick
    monkeypatch.setattr("hr_platform.session.tick", lambda *_: (_ for _ in ()).throw(error("stage failure")))
    assert run_at(session, 4)["errors"] == [{"stage": "decision", "error_class": error.__name__}]
    # Collection still executes even while the decision stage remains broken.
    result = run_at(session, 24)
    assert calls[-1] == "sample-4" and result["status"] == "INCOMPLETE"
    assert store.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0
    monkeypatch.setattr("hr_platform.session.tick", original)
    result = run_at(session, 25)
    assert all(d["status"] == "NO_BET" and d["reason"] == "DECISION_TOO_LATE" for d in result["decisions"])
    # A payout acquired before the late decision cannot be used to settle it.
    assert result["status"] == "INCOMPLETE" and result["settlements"] == []


def test_refusal_stops_http_but_fixed_decision_is_recorded(session):
    store, _, _, samples, calls = session
    samples.opener = lambda _: Response(b"denied", 403)
    assert run_at(session, -3)["captures"][0]["status"] == "SOURCE_DENIED"
    samples.opener = lambda _: pytest.fail("source refused")
    due = run_at(session, 4)
    assert all(d["status"] == "NO_BET" for d in due["decisions"])
    assert due["status"] == "INCOMPLETE" and due["next_at"] is None
    assert store.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_unqualified_payout_remains_unsettled_without_additional_requests(session):
    _, _, _, samples, calls = session
    for minute in [-3, -1, 1, 3, 4]:
        run_at(session, minute)
    samples.opener = lambda _: Response(b"results not published", kind="payout")
    result = run_at(session, 24)
    assert result["status"] == "INCOMPLETE" and result["settlements"] == []
    samples.opener = lambda _: pytest.fail("unplanned retry")
    assert run_at(session, 26) == result


def test_scope_mismatch_rejected_before_network(session):
    store, paper, capture, samples, calls = session
    invalid = deepcopy(capture)
    invalid["requests"][0]["scope"] = "20000102"
    with pytest.raises(ValueError, match="SESSION_CAPTURE_SCOPE"):
        step(store, paper["id"], invalid, samples=samples)
    assert calls == []


def test_cli_prints_only_private_location_and_rejects_ci(session, tmp_path, monkeypatch, capsys):
    from hr_platform import cli
    store, paper, capture, _, _ = session
    path = tmp_path / "session.json"
    path.write_text(json.dumps(capture))
    monkeypatch.setattr(cli, "private_root", lambda _: store.root)
    monkeypatch.setattr(cli, "Store", lambda _: Store(store.root, clock=lambda: f.at(-8)))
    args = ["paper-session", "--paper-plan", paper["id"], "--sample-plan", str(path)]
    assert cli.main(args) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"status", "private_report"}
    monkeypatch.setenv("CI", "true")
    assert cli.main(args) == 2
    assert "REAL_DATA_DISABLED_IN_CI" in capsys.readouterr().err


def test_due_slot_crossed_while_finishing_step_remains_immediately_actionable(session, monkeypatch):
    store, paper, capture, samples, calls = session
    original = __import__("hr_platform.session", fromlist=["tick"]).tick
    store.clock = lambda: f.at(-3, -1)
    def slow_tick(*args):
        result = original(*args)
        store.clock = lambda: f.at(-3, 1)
        return result
    monkeypatch.setattr("hr_platform.session.tick", slow_tick)
    result = step(store, paper["id"], capture, samples=samples)
    assert calls == [] and result["next_at"] == stamp(f.at(-3, 1))
    step(store, paper["id"], capture, samples=samples)
    assert calls == ["sample-0"]


def test_bounded_foreground_wait_drives_only_registered_slots_and_fixed_decision(session):
    from datetime import timedelta
    from hr_platform.common import instant
    from hr_platform.session import run
    store, paper, capture, samples, calls = session
    elapsed, sleeps = [0.0], []
    start = instant(f.at(-4))
    store.clock = lambda: (start + timedelta(seconds=elapsed[0])).isoformat()
    def sleeper(delay):
        assert 0 < delay <= 30
        sleeps.append(delay)
        elapsed[0] += delay
    result = run(store, paper["id"], capture, 540, samples=samples,
                 sleeper=sleeper, timer=lambda: elapsed[0])
    assert calls == [f"sample-{i}" for i in range(4)] and len(result["decisions"]) == 3
    assert all(d["status"] == "PAPER_BET" for d in result["decisions"])
    assert elapsed[0] <= 540 and sum(sleeps) == elapsed[0]
    assert result["next_at"] == stamp(f.at(24)) and result["settlements"] == []
    # A distant action does not keep the process alive for its entire wait budget.
    elapsed[0] = 600
    before = len(sleeps)
    run(store, paper["id"], capture, 300, samples=samples, sleeper=sleeper, timer=lambda: elapsed[0])
    assert len(sleeps) == before


def test_one_finite_run_reaches_payout_without_agent_restarts(session):
    from datetime import timedelta
    from hr_platform.common import instant
    from hr_platform.session import run
    store, paper, capture, samples, calls = session
    elapsed = [0.0]
    start = instant(f.at(-4))
    store.clock = lambda: (start + timedelta(seconds=elapsed[0])).isoformat()
    result = run(store, paper["id"], capture, 3600, samples=samples,
                 sleeper=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
                 timer=lambda: elapsed[0])
    assert result["status"] == "COMPLETE" and len(result["settlements"]) == 3
    assert calls == [f"sample-{i}" for i in range(5)]
    assert 900 < elapsed[0] <= 3600


def test_decision_cutoff_crossed_after_not_due_is_not_skipped(session, monkeypatch):
    store, paper, capture, samples, calls = session
    for minute in [-3, -1, 1, 3]:
        run_at(session, minute)
    original = __import__("hr_platform.session", fromlist=["tick"]).tick
    store.clock = lambda: f.at(4, -1)
    def slow_tick(*args):
        result = original(*args)
        store.clock = lambda: f.at(4, 1)
        return result
    monkeypatch.setattr("hr_platform.session.tick", slow_tick)
    result = step(store, paper["id"], capture, samples=samples)
    assert result["decisions"][0]["status"] == "NOT_DUE"
    assert result["next_at"] == stamp(f.at(4, 1))
    result = step(store, paper["id"], capture, samples=samples)
    assert len(result["decisions"]) == 3 and all(d["status"] == "PAPER_BET" for d in result["decisions"])
    assert len(calls) == 4


def test_unexpected_solver_failure_records_fixed_model_error_without_disabling_payout_capture(session, monkeypatch):
    from hr_platform.paper import decide
    def broken(*_):
        raise RuntimeError("synthetic solver failure")
    monkeypatch.setattr("hr_platform.paper.decide", lambda *a, **kw: decide(*a, **kw, analyzer=broken))
    for minute in [-3, -1, 1, 3]:
        run_at(session, minute)
    due = run_at(session, 4)
    assert all(d["status"] == "NO_BET" and d["reason"] == "MODEL_ERROR" for d in due["decisions"])
    result = run_at(session, 24)
    assert result["status"] == "COMPLETE" and result["decisions"] == due["decisions"]
    assert session[-1][-1] == "sample-4"
