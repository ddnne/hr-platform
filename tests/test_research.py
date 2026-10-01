from hr_platform import fixtures as f
from hr_platform.common import stamp
from hr_platform.research import trajectory, research_asof
import pytest


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



def test_reparse_removal_is_shared_by_asof_latest_and_trajectory(store):
    from hr_platform.parser import parse_odds

    store.ingest(f.event('a', 0), f.archive())
    before = trajectory(store, f.RACE, ['quinella'], f.at(3))
    def corrected(*args):
        races = parse_odds(*args)
        del races[f.RACE]['markets']['quinella']
        return races
    store.clock = lambda: f.at(4)
    store.reparse('a', 'synthetic-market-removal', corrected)
    assert trajectory(store, f.RACE, ['quinella'], f.at(3)) == before
    assert not trajectory(store, f.RACE, ['quinella'], f.at(4))['history']['quinella']
    assert store.latest(f.RACE, 'quinella') is None
    # Audit history still contains the withdrawn interpretation.
    assert len(store.history(f.RACE, 'quinella', f.at(4))) == 1


def test_window_keeps_all_observations_and_gaps_without_importing_old_reparses(store):
    raw = f.archive()
    store.plan([f.at(0), f.at(2), f.at(4), f.at(6)])
    store.ingest(f.event('old', 0), raw)
    store.ingest(f.event('first', 2), raw)
    store.clock = lambda: f.at(4)
    store.ingest(f.event('failed', 4, status=503))
    store.clock = lambda: f.at(6, 2)
    store.ingest(f.event('repeat', 6), raw)
    store.ingest(f.event('repeat', 6), raw)
    store.reparse('old', 'later-parse-of-old-receipt')
    before = trajectory(store, f.RACE, ['quinella'], f.at(7), since=f.at(2, 1))
    points = before['history']['quinella']
    assert [x['observation_id'] for x in points] == ['first', 'repeat']
    assert points[0]['raw_hash'] == points[1]['raw_hash']
    assert [x['slot'] for x in before['gaps']] == [stamp(f.at(4))]
    store.clock = lambda: f.at(8, 2)
    store.ingest(f.event('future', 8), f.archive(distorted=False))
    store.reparse('first', 'future-parser')
    assert trajectory(store, f.RACE, ['quinella'], f.at(7), since=f.at(2, 1)) == before
    assert len(store.history(f.RACE, 'quinella', f.at(7), since=f.at(2), current_only=True)) == 2


def test_reversed_history_window_is_rejected(store):
    with pytest.raises(ValueError, match='HISTORY_WINDOW_REVERSED'):
        store.history(f.RACE, 'quinella', f.at(2), since=f.at(3))
    with pytest.raises(ValueError, match='HISTORY_WINDOW_REVERSED'):
        trajectory(store, f.RACE, [], f.at(2), since=f.at(3))
