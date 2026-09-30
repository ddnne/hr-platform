"""Synthetic public-page captures, sharing the existing transport and store."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from hr_platform import fixtures as f
from hr_platform.common import instant, stamp
from hr_platform.public_probe import history, make_plan, validate_plan
from hr_platform.sampling import Samples, advance_plan, run_plan
from test_sampling import Response, plan as nar_plan

URL = "https://www.nankankeiba.com/odds/200001011901010108.do"
HTML = b"<html><body>SYNTHETIC ODDS PAGE</body></html>"


@pytest.fixture(autouse=True)
def private_environment(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def policy():
    return json.loads(Path("configs/public-probe.json").read_text())


def test_one_page_future_plan_pins_spacing_and_config(policy):
    plan = make_plan(URL, f.at(10), policy)
    assert validate_plan(plan) is None
    assert len(plan["requests"]) == 5
    assert {x["url"] for x in plan["requests"]} == {URL}
    assert plan["requests"][-1]["at"] == stamp(f.at(8, 50))
    plan["requests"][-1]["at"] = stamp(f.at(9))
    with pytest.raises(ValueError, match="PROBE_PLAN"):
        validate_plan(plan)
    policy["interval_seconds"] = 1
    with pytest.raises(ValueError, match="PROBE_POLICY"):
        make_plan(URL, f.at(10), policy)


@pytest.mark.parametrize("url", [URL + "?x=1", URL.replace("www.", "user@www."),
                                    URL.replace("nankankeiba.com", "other.example"),
                                    URL.replace("0108.do", "0110.do")])
def test_probe_uses_only_qualified_public_routes(policy, url):
    with pytest.raises(ValueError, match="PROBE_URL"):
        make_plan(url, f.at(10), policy)


def test_raw_repeat_is_new_capture_but_replay_has_no_request_or_paper(store, policy):
    calls = []
    plan = make_plan(URL, f.at(10), policy)
    samples = Samples.for_plan(store, plan, opener=lambda item: calls.append(item) or Response(HTML, kind="page"))
    samples.register(plan)
    results = []
    for item in plan["requests"][:2]:
        store.clock = lambda: item["at"]
        result = samples.capture(plan, item["id"])
        results.append(result)
        assert samples.capture(plan, item["id"]) == result
        assert result["status"] == "RAW_STORED" and result["raw_only"] and not result["paper_eligible"]
    assert len(calls) == 2
    assert results[0]["attempt_id"] != results[1]["attempt_id"]
    receipts = [json.loads(store.read_body(x["receipt_hash"], "receipts")) for x in results]
    assert receipts[0]["sha256"] == receipts[1]["sha256"]
    assert receipts[0]["collector_received_at"] < receipts[1]["collector_received_at"]
    assert all(x["source_updated_at"] is None for x in receipts)
    replay = history(store, plan)
    assert [r["receipt"] for r in replay["captures"][:2]] == receipts
    assert all(r["receipt"] is None for r in replay["captures"][2:])
    assert all(r["available_at"] is None for r in replay["captures"])
    assert store.metrics()["raw_objects"] == 1
    for table in ["observations", "parses", "decisions", "settlements", "plans"]:
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_provider_controls_share_probe_limits_without_weakening_nar(store, policy):
    plan = make_plan(URL, f.at(10), policy)
    other = make_plan(URL.replace("0108.do", "0103.do"), f.at(10), policy)
    calls = []
    def opener(item):
        return calls.append(item) or Response(HTML, kind="page")
    first = Samples.for_plan(store, plan, opener=opener)
    second = Samples.for_plan(store, other, opener=opener)
    first.register(plan)
    second.register(other)
    nar = Samples(store, lambda _: Response())
    n = nar_plan(minutes=(4, 8))
    nar.register(n)
    store.clock = lambda: f.at(4)
    assert nar.capture(n, "sample-0")["status"] == "PARSED"
    nar_before = dict(store.db.execute("SELECT * FROM sample_control WHERE id=1").fetchone())
    store.clock = lambda: plan["requests"][0]["at"]
    assert first.capture(plan, "before-310")["status"] == "RAW_STORED"
    assert second.capture(other, "before-310")["status"] == "WAIT"
    assert dict(store.db.execute("SELECT * FROM sample_control WHERE id=1").fetchone()) == nar_before
    first.control(stop=True)
    store.clock = lambda: f.at(8)
    assert nar.capture(n, "sample-1")["status"] == "PARSED"
    assert len(calls) == 1


@pytest.mark.parametrize("status,body,headers", [
    (403, b"denied", {}), (429, b"limited", {"Retry-After": "600"}),
    (200, b"CAPTCHA", {}),
])
def test_denial_or_wait_applies_to_other_pages_without_retries(store, policy, status, body, headers):
    plan = make_plan(URL, f.at(10), policy)
    calls = []
    samples = Samples.for_plan(store, plan, opener=lambda item: calls.append(item) or Response(body, status, kind="page", headers=headers))
    samples.register(plan)
    store.clock = lambda: plan["requests"][0]["at"]
    advance_plan(store, plan, samples=samples)
    store.clock = lambda: plan["requests"][1]["at"]
    result = advance_plan(store, plan, samples=samples)
    assert result["capture"]["status"] == ("WAIT" if status == 429 else "SOURCE_STOPPED")
    assert len(calls) == 1


def test_legacy_control_migration_preserves_nar_denial_and_deadline(store, policy):
    store.db.executescript("""CREATE TABLE sample_control(id INTEGER PRIMARY KEY CHECK(id=1),
        stopped INTEGER NOT NULL, next_at TEXT);
        INSERT INTO sample_control VALUES(1,1,'2000-01-02T00:00:00.000000+00:00');""")
    before = dict(store.db.execute("SELECT * FROM sample_control").fetchone())
    Samples(store, policy=policy)
    Samples(store)
    assert dict(store.db.execute("SELECT * FROM sample_control WHERE id=1").fetchone()) == before
    assert store.db.execute("SELECT stopped FROM sample_control WHERE id=2").fetchone()[0] == 0


def test_bounded_run_and_missed_slot_never_backfill(store, policy):
    now = instant(f.at(2, 2)).timestamp()
    beginning = now
    store.clock = lambda: datetime.fromtimestamp(now, timezone.utc).isoformat()
    def sleep(delay):
        nonlocal now
        now += delay
    plan = make_plan(URL, f.at(10), policy)
    calls = []
    samples = Samples.for_plan(store, plan, opener=lambda item: calls.append(item) or Response(HTML, kind="page"))
    samples.register(plan)
    now = instant(plan["requests"][1]["at"]).timestamp()
    result = run_plan(store, plan, policy["max_wait_seconds"], max_wait_seconds=policy["max_wait_seconds"],
                      sleep_seconds=policy["sleep_seconds"], samples=samples, sleeper=sleep, timer=lambda: now-beginning)
    assert result["status"] == "PLAN_ENDED"
    assert [x["id"] for x in calls] == [x["id"] for x in plan["requests"][1:]]
    assert advance_plan(store, plan, samples=samples)["capture"] == result["capture"]
    assert len(calls) == 4
