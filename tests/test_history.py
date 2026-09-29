import json
import pytest
from hr_platform import fixtures as f
from hr_platform.parser import parse_odds
from hr_platform.store import Store


def test_reobservations_redelivery_gaps_and_reopen(store):
    store.plan([f.at(m) for m in (0, 2, 4, 6, 8)])
    for name, minute, raw, status in [
        ("a", 0, f.archive(), 200),
        ("a2", 2, f.archive(), 200),
        ("failure", 4, None, 503),
        ("b", 6, f.archive("nonuniform"), 200),
    ]:
        store.clock = lambda m=minute: f.at(m, 2)
        store.ingest(f.event(name, minute, status), raw)
    store.ingest(f.event("b", 6), f.archive("nonuniform"))
    assert store.metrics()["observations"] == 3
    assert store.metrics()["raw_objects"] == 2
    at4 = store.asof(f.RACE, ["quinella"], f.at(4, 2))
    assert at4["markets"]["quinella"]["observation_id"] == "a2"
    assert at4["markets"]["quinella"]["age_seconds"] == 121
    assert at4["gaps"][-1]["slot"] == f.at(4).replace("+00:00", ".000000+00:00")
    assert store.gaps(f.at(8))[-1]["reason"] == "NOT_EXECUTED"
    assert len(store.history(f.RACE, "quinella")) == 3
    assert store.asof(f.RACE, ["quinella"], f.at(-1))["reason"] == "DATA_MISSING"
    assert store.asof(f.RACE, ["quinella"], f.at(15))["reason"] == "STALE"
    reopened = Store(store.root)
    assert reopened.asof(f.RACE, ["quinella"], f.at(4))["markets"]["quinella"]["observation_id"] == "a2"
    assert reopened.latest(f.RACE, "quinella")["observation_id"] == "b"
    reopened.close()


def test_reparse_preserves_failure_and_availability(store):
    def broken(*_):
        raise ValueError("schema error")

    store.ingest(f.event("a", 0), f.archive(), "broken-v1", broken)
    assert store.metrics()["observations"] == 1
    assert store.asof(f.RACE, ["quinella"], f.at(3))["reason"] == "DATA_MISSING"
    store.clock = lambda: f.at(10)
    store.reparse("a", "repaired-v2")
    assert store.metrics()["parse_errors"] == 1
    assert store.asof(f.RACE, ["quinella"], f.at(3))["reason"] == "DATA_MISSING"
    assert store.asof(f.RACE, ["quinella"], f.at(10))["reason"] == "STALE"
    available = store.history(f.RACE, "quinella")[0]["available_at"]
    store.clock = lambda: f.at(30)
    store.reparse("a", "repaired-v2")
    assert store.history(f.RACE, "quinella")[0]["available_at"] == available


def test_final_only_never_enters_before_or_after(store):
    store.ingest(f.event("final", 0, kind="FINAL_ONLY"), f.archive())
    assert store.asof(f.RACE, ["quinella"], f.at(3))["reason"] == "DATA_MISSING"
    assert store.latest(f.RACE, "quinella")["event"]["dataset_kind"] == "FINAL_ONLY"


def test_conflicting_delivery(store):
    store.ingest(f.event("a", 0), f.archive())
    with pytest.raises(ValueError, match="EVENT_ID_CONFLICT"):
        store.ingest(f.event("a", 0), f.archive("nonuniform"))


def test_304_validator_and_unknown(store):
    store.ingest(f.event("a", 0), f.archive())
    store.clock = lambda: f.at(2, 2)
    store.ingest(f.event("b", 2, 304, validator_attempt="a", validator="a"))
    assert store.metrics()["observations"] == 2
    assert store.metrics()["raw_objects"] == 1
    assert store.latest(f.RACE, "win")["basis"] == "validator_304"
    with pytest.raises(ValueError, match="UNBOUND_304"):
        store.ingest(f.event("bad", 2, 304, validator_attempt="a", validator="WRONG"))


def test_partial_index_failure_reuses_raw(store, monkeypatch):
    original = store.reparse
    monkeypatch.setattr(store, "reparse", lambda *_: (_ for _ in ()).throw(OSError("index unavailable")))
    with pytest.raises(OSError):
        store.ingest(f.event("a", 0), f.archive())
    assert store.metrics()["raw_objects"] == 1
    assert store.asof(f.RACE, ["win"], f.at(3))["reason"] == "DATA_MISSING"
    monkeypatch.setattr(store, "reparse", original)
    store.ingest(f.event("a", 0), f.archive())
    assert store.metrics()["observations"] == 1
    assert store.asof(f.RACE, ["win"], f.at(3))["reason"] is None


def test_raw_write_failure_does_not_publish(store, monkeypatch):
    monkeypatch.setattr(store, "body", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.ingest(f.event("a", 0), f.archive())
    assert store.metrics()["attempts"] == 1
    assert store.metrics()["observations"] == 0


def test_filename_timestamp_is_not_source_time(store):
    store.ingest(f.event("a", 0, file_timestamp=f.at(99)), f.archive())
    item = store.latest(f.RACE, "win")
    assert item["content"]["source_updated_at"] is None
    assert item["event"]["file_timestamp"] == f.at(99)


def test_later_reparse_does_not_replace_newer_observation(store):
    store.ingest(f.event("a", 0), f.archive())
    store.clock = lambda: f.at(6, 2)
    store.ingest(f.event("b", 6), f.archive("nonuniform"))
    store.clock = lambda: f.at(8)
    store.reparse("a", "v2", parse_odds)
    assert store.asof(f.RACE, ["win"], f.at(8))["markets"]["win"]["observation_id"] == "b"
    assert (
        json.loads(store.db.execute('SELECT event FROM attempts WHERE id="a"').fetchone()[0])[
            "source_updated_at"
        ]
        is None
    )


@pytest.mark.parametrize('remove', ['market', 'race'])
def test_reparse_removal_does_not_resurrect_old_market_or_race(store, remove):
    store.ingest(f.event('a', 0), f.archive())
    before = store.asof(f.RACE, ['win', 'quinella'], f.at(3))
    def corrected(*args):
        races = parse_odds(*args)
        if remove == 'market':
            del races[f.RACE]['markets']['quinella']
        else:
            del races[f.RACE]
        return races
    store.clock = lambda: f.at(4)
    store.reparse('a', 'synthetic-correction', corrected)
    assert store.asof(f.RACE, ['win', 'quinella'], f.at(3)) == before
    after = store.asof(f.RACE, ['win', 'quinella'], f.at(4))
    assert after['reason'] == 'DATA_MISSING' and 'quinella' not in after['markets']
    if remove == 'race':
        assert not after['markets']


def test_retry_never_erases_original_missing_slot(store):
    store.plan([f.at(0)])
    store.ingest(f.event("failure", 0, 503))
    original = store.gaps(f.at(3))
    store.clock = lambda: f.at(6, 2)
    store.ingest(f.event("retry", 6, scheduled_capture_at=f.at(0), retry_of="failure"), f.archive())
    assert store.gaps(f.at(7)) == original
    assert store.gaps(f.at(3)) == original


def test_publication_interruption_recovers_at_recovery_time(store):
    moments = iter([f.at(0, 2), f.at(0, 3)])
    store.clock = lambda: next(moments)
    with pytest.raises(StopIteration):
        store.ingest(f.event("a", 0), f.archive())
    assert store.asof(f.RACE, ["win"], f.at(1))["reason"] == "DATA_MISSING"
    store.clock = lambda: f.at(5)
    store.ingest(f.event("a", 0), f.archive())
    assert store.asof(f.RACE, ["win"], f.at(1))["reason"] == "DATA_MISSING"
    assert store.asof(f.RACE, ["win"], f.at(5))["reason"] is None
    assert store.latest(f.RACE, "win")["available_at"].startswith("2000-01-01T05:05:00")
