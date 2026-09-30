"""Synthetic day capture and race-watch checks, using the existing fake transport."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from hr_platform import fixtures as f
from hr_platform.common import instant, stamp
from hr_platform.day_collection import make_plan, race_watch, run, step
from hr_platform.sampling import Samples, validate_plan
from test_realdata import race_archive
from test_sampling import Response, plan as sample_plan


@pytest.fixture(autouse=True)
def private_environment(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def settings():
    return json.loads(Path("configs/collection.json").read_text())


def test_full_day_is_one_file_per_slot_and_separate_from_small_sample_limit(settings):
    plan = make_plan("2000-01-01T09:00:00+09:00", "2000-01-01T21:00:00+09:00", settings)
    assert len(plan["requests"]) == 360
    assert len({x["url"] for x in plan["requests"]}) == 1
    assert {x["kind"] for x in plan["requests"]} == {"odds"}
    assert validate_plan(plan)
    plan["format"] = "nar-finite-local-v1"
    with pytest.raises(ValueError, match="SAMPLE_PLAN_LIMIT"):
        validate_plan(plan)
    with pytest.raises(ValueError, match="DAY_PLAN_DATE"):
        make_plan("2000-01-01T23:58:00+09:00", "2000-01-02T00:01:00+09:00", settings)


def test_day_reuses_shared_control_keeps_repeat_observations_and_skips_missed_slots(store, settings):
    calls = []
    samples = Samples(store, lambda item: calls.append(item) or Response())
    plan = make_plan(f.at(4), f.at(14), settings)
    assert step(store, plan, samples=samples)["status"] == "WAITING"
    store.clock = lambda: f.at(4)
    first = step(store, plan, samples=samples)
    assert first["capture"]["status"] == "PARSED"
    assert step(store, plan, samples=samples)["capture"] == first["capture"]
    store.clock = lambda: f.at(6)
    assert step(store, plan, samples=samples)["capture"]["status"] == "PARSED"
    before = store.asof(f.RACE, ["quinella"], f.at(7))
    # The missed 8/10-minute slots remain missing on restart at minute 12.
    store.clock = lambda: f.at(12)
    report = step(store, plan, samples=samples)
    assert report["capture"]["status"] == "PARSED" and len(calls) == 3
    assert store.metrics()["observations"] == 3 and store.metrics()["raw_objects"] == 1
    assert store.asof(f.RACE, ["quinella"], f.at(7)) == before
    assert {x["slot"] for x in store.gaps(f.at(12))} >= {stamp(f.at(8)), stamp(f.at(10))}
    with pytest.raises(ValueError, match="SAMPLE_PLAN_TOO_LATE"):
        step(store, make_plan(f.at(3), f.at(5), settings), samples=samples)


@pytest.mark.parametrize("status,expected", [(403, "SOURCE_STOPPED"), (429, "WAITING")])
def test_day_refusal_and_retry_after_are_shared_with_finite_samples(store, settings, status, expected):
    calls = []
    samples = Samples(store, lambda item: calls.append(item) or Response(b"refusal", status, headers={"Retry-After": "600"}))
    plan = make_plan(f.at(4), f.at(20), settings)
    other = sample_plan(("race",), (8,))
    samples.register(other)
    samples.register(plan)
    store.clock = lambda: f.at(4)
    assert step(store, plan, samples=samples)["status"] == expected
    store.clock = lambda: f.at(6)
    step(store, plan, samples=samples)
    store.clock = lambda: f.at(8)
    assert samples.capture(other, "sample-0")["status"] in {"SOURCE_STOPPED", "WAIT"}
    assert len(calls) == 1


def test_race_watch_retains_per_market_clocks_and_never_qualifies_paper(store, settings):
    samples = Samples(store, lambda item: Response(race_archive(), kind="race") if item["kind"] == "race" else Response())
    metadata = sample_plan(("race",), (4,))
    plan = make_plan(f.at(6), f.at(10), settings)
    samples.register(metadata)
    samples.register(plan)
    store.clock = lambda: f.at(4)
    samples.capture(metadata, "sample-0")
    assert race_watch(store, "20000101", f.at(5), settings)["races"][0]["observations"] == {}
    store.clock = lambda: f.at(6)
    step(store, plan, samples=samples)
    before = race_watch(store, "20000101", f.at(7), settings)
    race = before["races"][0]
    assert race["race_id"] == f.RACE and not race["paper_eligible"]
    assert race["coverage"]["markets"]["exacta"]["missing"] == []
    assert race["observations"]["exacta"]["received_at"] == stamp(f.at(6))
    assert not race["stale_markets"]
    store.clock = lambda: f.at(8)
    step(store, plan, samples=samples)
    assert race_watch(store, "20000101", f.at(7), settings) == before
    assert "exacta" in race_watch(store, "20000101", f.at(15), settings)["races"][0]["stale_markets"]
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_day_wait_is_bounded_and_resumes_next_slot_without_model(store, settings):
    now = instant(f.at(2, 2)).timestamp()
    start = now
    store.clock = lambda: datetime.fromtimestamp(now, timezone.utc).isoformat()
    def sleep(delay):
        nonlocal now
        now += delay
    samples = Samples(store, lambda _: Response())
    report = run(store, make_plan(f.at(4), f.at(10), settings), settings, 480,
                 samples=samples, sleeper=sleep, timer=lambda: now - start)
    assert report["status"] == "PLAN_ENDED"
    assert store.metrics()["observations"] == 3


def test_slow_parse_keeps_the_next_still_open_slot(store, settings, monkeypatch):
    samples = Samples(store, lambda _: Response())
    plan = make_plan(f.at(4), f.at(8), settings)
    samples.register(plan)
    publish = samples.publish
    def slow_publish(item, receipt):
        result = publish(item, receipt)
        store.clock = lambda: f.at(6, 10)
        return result
    monkeypatch.setattr(samples, "publish", slow_publish)
    store.clock = lambda: f.at(4)
    first = step(store, plan, samples=samples)
    assert first["next_at"] == stamp(f.at(6, 10))
    second = step(store, plan, samples=samples)
    assert second["capture"]["status"] == "PARSED"
    assert store.metrics()["observations"] == 2
