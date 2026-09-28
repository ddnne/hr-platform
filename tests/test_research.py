from hr_platform import fixtures as f
from hr_platform.research import trajectory, research_asof


def test_observations_not_revisions_and_no_future_or_final(store):
    raw = f.archive()
    store.ingest(f.event("a", 0), raw)
    store.ingest(f.event("a", 0), raw)
    store.clock = lambda: f.at(3)
    store.ingest(f.event("b", 2), raw)
    store.reparse("a", "second-parser")
    store.clock = lambda: f.at(3, 30)
    store.ingest(f.event("final", 3, kind="FINAL_ONLY"), raw)
    store.clock = lambda: f.at(6)
    store.ingest(f.event("future", 5), f.archive(distorted=False))
    store.reparse("a", "late-parser")
    report = trajectory(store, f.RACE, ["quinella"], f.at(4))
    points = report["history"]["quinella"]
    assert [x["observation_id"] for x in points] == ["a", "b"]
    assert points[0]["version"] == "second-parser"
    assert points[0]["raw_hash"] == points[1]["raw_hash"]
    assert all(x["content"]["source_updated_at"] is None for x in points)
    assert report["interpolated"] is False


def test_failed_slot_is_not_interpolated(store):
    store.plan([f.at(0), f.at(2)])
    store.ingest(f.event("a", 0), f.archive())
    store.ingest(f.event("failure", 2, status=503))
    report = trajectory(store, f.RACE, ["quinella"], f.at(4))
    assert len(report["history"]["quinella"]) == 1
    assert any(x["reason"] == "FAILED_OR_NOT_AVAILABLE" for x in report["gaps"])


def test_common_cutoff_analysis_without_paper(collected, config):
    report = research_asof(collected, f.RACE, f.schedule(), config)
    assert report["status"] == "ANALYZED"
    assert report["live_execution_verified"] is False
    assert report["analysis"]["rows"]
    assert {x["observation_id"] for x in report["view"]["markets"].values()} == {"a"}
    for table in ("decisions", "settlements"):
        assert collected.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_schedule_learned_late_never_runs_model(collected, config):
    schedule = {**f.schedule(), "known_at": f.at(5)}
    def forbidden(*args):
        raise AssertionError("must not fit")
    report = research_asof(collected, f.RACE, schedule, config, forbidden)
    assert report["reason"] == "SCHEDULE_NOT_KNOWN"
    assert "analysis" not in report


def test_daily_final_state_and_unknown_are_separated(store):
    for name, status in [("final", "FINAL"), ("unknown", "UNKNOWN")]:
        event = f.event(name, 0, kind="DAILY_SNAPSHOT")
        event["race_states"][f.RACE]["status"] = status
        store.ingest(event, f.archive())
    report = trajectory(store, f.RACE, ["quinella"], f.at(4))
    assert report["history"]["quinella"] == []
    assert len(report["excluded_points"]["quinella"]) == 2
    assert all(x["exclusion_reason"] == "RACE_NOT_PRE_RACE"
               for x in report["excluded_points"]["quinella"])
