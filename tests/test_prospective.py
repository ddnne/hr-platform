from copy import deepcopy
import json
import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical, sha, stamp
from hr_platform.paper import decide
from hr_platform.prospective import POLICY, SCHEMA, build_view, configuration, current_plan, enroll, tick
from hr_platform.race_metadata import MetadataEvidence
from hr_platform.race_state import StateEvidence
from hr_platform.store import Store
from test_race_state import page, receipt
from test_realdata import race_archive


def metadata_receipt(raw, minute):
    return {**receipt(raw, minute),
            "url": "https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload?type=daily",
            "filename": "20000101_0946706400_race.zip"}


def metadata(store, minute, raw=None, **kwargs):
    raw = raw or race_archive()
    store.clock = lambda: f.at(minute, 10)
    return MetadataEvidence(store).ingest(metadata_receipt(raw, minute), raw, "20000101", **kwargs)


def state(store, minute, stage="14:02現在", change="", start="14:14"):
    raw = page(stage, change).replace(b"14:10", start.encode())
    store.clock = lambda: f.at(minute, 10)
    return StateEvidence(store).ingest(receipt(raw, minute), raw, f.RACE)


@pytest.fixture
def planned(store, config):
    metadata(store, -2)
    plan = enroll(store, f.RACE, config)
    # All decision inputs are genuinely observed/published before the cutoff.
    metadata(store, 2)
    state(store, 2)
    store.ingest(f.event("observed", 2, race_states={}), f.archive())
    store.clock = lambda: f.at(4, 20)
    return store, plan


def test_metadata_repeat_reparse_and_removed_race_do_not_rewrite_history(store):
    first = metadata(store, -2)
    assert first["source_updated_at"] is None and not first["paper_eligible"]
    evidence = MetadataEvidence(store)
    before = evidence.for_race(f.RACE, f.at(0))
    second = metadata(store, 2)
    assert first["raw_hash"] == second["raw_hash"]
    assert first["observation_id"] != second["observation_id"]
    assert metadata(store, 2) == second
    store.clock = lambda: f.at(8)
    raw = race_archive()
    removed = evidence.ingest(metadata_receipt(raw, 2), raw, "20000101",
                              version="synthetic-removed", parser=lambda *_: {"races": {}})
    assert removed["available_at"] == stamp(f.at(8))
    assert evidence.for_race(f.RACE, f.at(8))["evidence"]["metadata"] is None
    assert evidence.for_race(f.RACE, f.at(0)) == before
    assert len(evidence.history("20000101", f.at(8))["history"]) == 3
    assert store.db.execute("SELECT count(*) FROM race_metadata_observations").fetchone()[0] == 2


@pytest.mark.parametrize("change", [
    {"status": 403}, {"sha256": "0" * 64}, {"headers_received_at": None},
    {"collector_received_at": f.at(9)}, {"filename": "200001_0946706400_race.zip"},
    {"filename": "20000102_0946706400_race.zip"},
    {"url": "https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload?type=monthly"},
])
def test_metadata_invalid_receipt_creates_no_observation(store, change):
    raw = race_archive()
    evidence = MetadataEvidence(store)
    with pytest.raises(ValueError):
        evidence.ingest({**metadata_receipt(raw, 0), **change}, raw, "20000101")
    assert store.db.execute("SELECT count(*) FROM race_metadata_observations").fetchone()[0] == 0


def test_cross_date_bundle_is_quarantined_without_fallback(store):
    raw = race_archive()
    r = metadata_receipt(raw, 0)
    r["filename"] = "20000102_0946706400_race.zip"
    result = MetadataEvidence(store).ingest(r, raw, "20000102")
    assert result["status"] == "QUARANTINED"
    assert store.read_body(result["raw_hash"], "raw") == raw


def test_metadata_csv_limit_is_recorded_and_never_falls_back(store, tmp_path, capsys):
    import argparse
    import csv
    import io
    import zipfile
    from hr_platform.cli import run

    metadata(store, 0)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(race_archive())) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith("_racelist.csv"):
                data = data.replace(b"SYNTHETIC", b"A" * (csv.field_size_limit() + 1))
            target.writestr(name, data)
    raw = output.getvalue()
    archive, r = tmp_path / "large.zip", tmp_path / "receipt.json"
    archive.write_bytes(raw)
    r.write_text(json.dumps(metadata_receipt(raw, 2)))
    store.clock = lambda: f.at(3)
    result = run(argparse.Namespace(command="import-metadata", zip=str(archive),
                                    receipt=str(r), date="20000101"), store)
    assert result["status"] == "QUARANTINED"
    evidence = MetadataEvidence(store).for_race(f.RACE, f.at(3))["evidence"]
    assert evidence["status"] == "QUARANTINED" and evidence["metadata"] is None
    assert len(MetadataEvidence(store).history("20000101", f.at(3))["history"]) == 2
    assert capsys.readouterr().out == ""


def test_all_three_models_use_same_derived_input_and_keep_raw_facts(planned):
    store, plan = planned
    original = store.asof(f.RACE, ["quinella", "win", "exacta"], plan["asof_at"])
    records = tick(store, plan["id"])["decisions"]
    assert len(records) == 3 and all(r["status"] == "PAPER_BET" for r in records)
    assert len({sha(canonical(r["input_view"])) for r in records}) == 1
    for record in records:
        view = record["input_view"]
        assert POLICY["runner_assumption"] in record["research_assumptions"]
        assert record["real_stake_yen"] == 0
        assert view["assembled_at"] > plan["asof_at"]
        for market in view["markets"].values():
            assert market["content"]["source_updated_at"] is None
            assert market["observed_state"]["status"] == "UNKNOWN"
            assert market["observed_body_hash"] != market["body_hash"]
            assert sha(store.read_body(market["body_hash"], "normalized")) == market["body_hash"]
        assert all(r["active"] is None for r in view["state_evidence"]["evidence"]["runners"].values())
    assert store.asof(f.RACE, ["quinella", "win", "exacta"], plan["asof_at"]) == original
    assert tick(store, plan["id"])["decisions"] == records
    reopened = Store(store.root, clock=lambda: f.at(4, 30))
    assert tick(reopened, plan["id"])["decisions"] == records
    reopened.close()


@pytest.mark.parametrize("stage,change,reason", [
    ("最終", "", "RACE_NOT_PRE_RACE"),
    ("不明", "", "RACE_NOT_PRE_RACE"),
    ("14:02現在", "競走除外", "RUNNER_CHANGE_OR_UNKNOWN"),
    ("14:02現在", "取消", "RUNNER_CHANGE_OR_UNKNOWN"),
    ("14:02現在", "不明変更", "RUNNER_CHANGE_OR_UNKNOWN"),
])
def test_final_and_changes_are_no_bet(planned, stage, change, reason):
    store, plan = planned
    state(store, 3, stage, change)
    store.clock = lambda: f.at(4, 20)
    assert all(r["reason"] == reason for r in tick(store, plan["id"])["decisions"])


@pytest.mark.parametrize("mutation,reason", [
    ("result", "RESULT_PRESENT"), ("roster", "ENTRY_COUNT_MISMATCH"),
    ("schedule", "SCHEDULE_CHANGED"), ("stale_metadata", "STATE_OR_METADATA_STALE"),
    ("stale_state", "STATE_OR_METADATA_STALE"),
    ("missing_market", "DATA_MISSING"), ("zero", "DATA_MISSING"),
    ("missing_selection", "DATA_MISSING"), ("late", "DECISION_TOO_LATE"),
])
def test_inputs_are_checked_independently(planned, mutation, reason):
    store, plan = planned
    if mutation == "result":
        metadata(store, 3, race_archive(finished=True))
    elif mutation == "roster":
        metadata(store, 3, race_archive(horse_count=3))
    elif mutation == "schedule":
        state(store, 3, start="14:15")
    elif mutation.startswith("stale_"):
        # Remove only the fresh evidence in a synthetic setup, retaining the earlier receipt.
        if mutation == "stale_metadata":
            table = "race_metadata"
        else:
            table = "race_state"
            state(store, -2)
        with store.db:
            store.db.execute(f"DELETE FROM {table}_parses WHERE observation_id IN "
                             f"(SELECT id FROM {table}_observations WHERE received_at>?)", (stamp(f.at(0)),))
    elif mutation in {"missing_market", "zero", "missing_selection"}:
        from hr_platform.parser import parse_odds

        def parser(raw, states, _):
            result = parse_odds(raw, states)
            markets = result[f.RACE]["markets"]
            if mutation == "missing_market":
                del markets["exacta"]
            elif mutation == "zero":
                markets["exacta"]["quotes"]["1-2"]["odds"] = 0
            else:
                del markets["exacta"]["quotes"]["1-2"]
            return result
        store.clock = lambda: f.at(3)
        store.reparse("observed", "synthetic-incomplete", parser)
    store.clock = lambda: f.at(7) if mutation == "late" else f.at(4, 20)
    assert all(r["reason"] == reason for r in tick(store, plan["id"])["decisions"])


def test_later_results_and_exclusions_cannot_rewrite_decision(planned):
    store, plan = planned
    before = build_view(store, f.RACE, plan["schedule"], plan["config"], plan["asof_at"])
    records = tick(store, plan["id"])["decisions"]
    metadata(store, 5, race_archive(finished=True))
    state(store, 5, "最終", "競走除外")
    after = build_view(store, f.RACE, plan["schedule"], plan["config"], plan["asof_at"])
    assert {k: v for k, v in before.items() if k != "assembled_at"} == {
        k: v for k, v in after.items() if k != "assembled_at"}
    assert tick(store, plan["id"])["decisions"] == records
    assert store.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_early_plan_replay_config_freeze_and_no_retrospective_enrollment(store, config):
    metadata(store, -2)
    plan = enroll(store, f.RACE, config)
    assert tick(store, plan["id"])["decisions"][0]["status"] == "NOT_DUE"
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    changed = deepcopy(config)
    changed["lambda"] *= 2
    with pytest.raises(ValueError, match="EXPERIMENT_CONFIG_CHANGED"):
        enroll(store, f.RACE, changed)
    with pytest.raises(ValueError, match="EXPERIMENT_CONFIG_CHANGED"):
        enroll(store, "20000101:SYNTHETIC:2", changed)
    assert enroll(store, f.RACE, config) == plan
    metadata(store, 4)
    config["version"] += "-another"
    with pytest.raises(ValueError, match="TOO_LATE"):
        enroll(store, f.RACE, config)
    config["mode"] = "FROZEN_PAPER"
    with pytest.raises(ValueError, match="SHADOW_ONLY"):
        enroll(store, f.RACE, config)


def test_unenrolled_policy_cannot_create_backfilled_bet(collected, config):
    records = decide(collected, f.RACE, f.schedule(), configuration(config))
    assert all(r["reason"] == "PROSPECTIVE_PLAN_REQUIRED" for r in records)


def test_enrollment_storage_crossing_cutoff_is_never_backdated(store, config):
    metadata(store, 2)
    store.db.executescript(SCHEMA)
    clock = [f.at(3, 59)]
    store.clock = lambda: clock[0]

    def delay():
        clock[0] = f.at(4, 1)
        return 1

    store.db.create_function("delay_registration", 0, delay)
    store.db.executescript("""CREATE TRIGGER delayed_plan AFTER INSERT ON paper_plans
                           BEGIN SELECT delay_registration(); END;""")
    plan = enroll(store, f.RACE, config)
    assert plan["registered_at"] == stamp(f.at(4, 1))
    assert plan["status"] == "REGISTRATION_TOO_LATE"
    assert all(r["reason"] == "PROSPECTIVE_PLAN_REQUIRED" for r in tick(store, plan["id"])["decisions"])


@pytest.mark.parametrize("start,late", [("1420", False), ("1412", True)])
def test_schedule_revisions_known_before_decision_and_no_repeat_after_decision(planned, start, late):
    store, plan = planned
    from hr_platform.race_files import parse_race_bundle

    def changed(raw, date):
        result = parse_race_bundle(raw)
        result["races"][f.RACE]["scheduled_start_at"] = f.at(int(start[-2:]))
        return result

    store.clock = lambda: f.at(3)
    raw = race_archive()
    MetadataEvidence(store).ingest(metadata_receipt(raw, 2), raw, "20000101",
                                   version="synthetic-schedule-change", parser=changed)
    updated = tick(store, plan["id"])
    new = current_plan(store, plan["id"])
    assert new["revision_id"] != plan["revision_id"] and new["supersedes"] == plan["revision_id"]
    assert store.db.execute("SELECT count(*) FROM paper_plans").fetchone()[0] == 2
    if late:
        assert updated["plan_status"] == "REGISTRATION_TOO_LATE"
        assert all(d["reason"] == "PROSPECTIVE_PLAN_REQUIRED" for d in updated["decisions"])
    else:
        assert updated["decisions"][0]["status"] == "NOT_DUE"
        # At the new cutoff, independently refreshed inputs agree with the new schedule.
        metadata(store, 8, version="synthetic-schedule-change", parser=changed)
        state(store, 8, start="14:20")
        store.ingest(f.event("new-observed", 8, race_states={}), f.archive())
        store.clock = lambda: f.at(10, 20)
        records = tick(store, plan["id"])["decisions"]
        assert all(d["status"] == "PAPER_BET" and d["asof_at"] == stamp(f.at(10)) for d in records)
        metadata(store, 12)
        assert tick(store, plan["id"])["decisions"] == records
        assert current_plan(store, plan["id"]) == new


def test_future_plan_revision_cannot_change_old_asof_before_any_decision(planned):
    store, plan = planned
    before = build_view(store, f.RACE, plan["schedule"], plan["config"], plan["asof_at"])
    from hr_platform.race_files import parse_race_bundle

    def delayed(raw, _):
        bundle = parse_race_bundle(raw)
        bundle["races"][f.RACE]["scheduled_start_at"] = f.at(20)
        return bundle

    metadata(store, 5, version="synthetic-delayed", parser=delayed)
    assert tick(store, plan["id"])["decisions"][0]["status"] == "NOT_DUE"
    after = build_view(store, f.RACE, plan["schedule"], plan["config"], plan["asof_at"])
    assert before["reason"] is None and after["reason"] is None
    assert {k: v for k, v in before.items() if k != "assembled_at"} == {
        k: v for k, v in after.items() if k != "assembled_at"}
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_cli_metadata_and_plan_outputs_stay_private(tmp_path, monkeypatch, capsys, config):
    from hr_platform import cli
    import yaml

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "private_root", lambda _: tmp_path / "store")
    monkeypatch.setattr(cli, "Store", lambda root: Store(root, clock=lambda: f.at(-1)))
    raw = race_archive()
    archive = tmp_path / "race.zip"
    archive.write_bytes(raw)
    received = tmp_path / "receipt.json"
    received.write_text(json.dumps(metadata_receipt(raw, -2)))
    assert cli.main(["import-metadata", "--zip", str(archive), "--receipt", str(received),
                     "--date", "20000101"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {"status", "private_report"}
    c = tmp_path / "config.yaml"
    c.write_text(yaml.safe_dump(config))
    assert cli.main(["paper-plan", "--race", f.RACE, "--config", str(c)]) == 0
    assert f.RACE not in capsys.readouterr().out
