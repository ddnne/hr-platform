import csv
import io
import json
import zipfile
import pytest
from hr_platform import fixtures as f
from hr_platform.cli import main, private_root
from hr_platform.common import instant, sha, stamp
from hr_platform.parser import parse_odds
from hr_platform.race_files import RACE_HEADERS, HORSE_HEADERS, PAYOUT_HEADERS, parse_race_bundle
from hr_platform.realdata import RealData, completeness, validate_manifest, filename_metadata
from hr_platform.store import Store


def test_monthly_inspection_is_final_only_and_never_creates_observations(tmp_path):
    from test_parser import monthly_archive
    from hr_platform.parser import MONTHLY_VERSION
    store = Store(tmp_path / 'private-monthly', clock=lambda: f.at(5))
    try:
        report = RealData(store).inspect(monthly_archive(), '200001_0946680000_odds.zip', 'FINAL_ONLY', 'utf-8-sig')
        assert report['status'] == 'PARSED_UNQUALIFIED' and report['recipe']['odds_version'] == MONTHLY_VERSION
        assert set(report['content']) == {f.RACE, f.RACE.replace('20000101', '20000102')}
        assert not report['paper_eligible'] and not report['live_qualified']
        assert store.db.execute('SELECT count(*) FROM observations').fetchone()[0] == 0
    finally:
        store.close()


def race_archive(*, finished=False, payout_rows=None, encoding="utf-8-sig", horse_count=4, popularity=False, start="1414"):
    key = {"競馬場": "SYNTHETIC", "競走年月日": "20000101", "レース番号": "1"}
    race = {**key, "発走時刻": start, "芝ダート区分": "ダート", "頭数": "4"}
    if finished:
        race["上がり3F"] = "38.2"
    horse = [
        {**key, "馬番": str(n), "枠番": str(n), "着順": str(n) if finished else "",
         "人気": str(n) if popularity else ""}
        for n in range(1, horse_count + 1)
    ]
    paybacks = [{**key, **r} for r in (payout_rows or [])]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for suffix, headers, rows in [
            ("racelist", RACE_HEADERS, [race]),
            ("horselist", HORSE_HEADERS, horse),
            ("payback", PAYOUT_HEADERS, paybacks),
        ]:
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, headers)
            writer.writeheader()
            writer.writerows(rows)
            entry = zipfile.ZipInfo(f"20000101_{suffix}.csv", (2000, 1, 1, 0, 0, 0))
            z.writestr(entry, text.getvalue().encode(encoding))
    return output.getvalue()


def manifest(minute=0, raw=None, **overrides):
    raw = f.archive() if raw is None else raw
    slot = int(instant(f.at(minute)).timestamp() * 1000)
    unix = int(instant(f.at(minute)).timestamp())
    # Filename may legitimately be much newer than an embedded race; never infer a row update from it.
    unix = 1790580000 + minute * 60
    result = {
        "event_id": f"nar-daily-odds:{slot}",
        "scheduled_capture_at": f.at(minute),
        "fetch_started_at": f.at(minute),
        "headers_received_at": f.at(minute, 1),
        "collector_received_at": f.at(minute, 2),
        "raw_saved_at": f.at(minute, 3),
        "raw_sha256": sha(raw),
        "raw_bytes": len(raw),
        "http_status": 200,
        "etag": '"v1"',
        "validator_sent": None,
        "validator_raw_sha256": None,
        "file_name": f"20260928_{unix}_odds.zip",
        "file_timestamp": __import__("datetime")
        .datetime.fromtimestamp(unix, __import__("datetime").timezone.utc)
        .isoformat(),
        "duration_ms": 3000,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp932"])
def test_race_bundle_separates_results_and_never_guesses_eligibility(encoding):
    pre = parse_race_bundle(race_archive(encoding=encoding), encoding)["races"][f.RACE]
    assert pre["scheduled_start_at"] == "2000-01-01T14:14:00+09:00"
    assert pre["status"] == "UNKNOWN" and pre["pre_race_evidence"] is None
    assert not pre["result_present"] and pre["entry_count_matches"]
    assert all(h["active"] is None for h in pre["horses"].values())
    final = parse_race_bundle(race_archive(finished=True))["races"][f.RACE]
    assert final["result_present"] and final["race_results"]["上がり3F"] == "38.2"
    assert not final["final"] and final["complete_markets"] == []


def test_dead_heat_rows_deduplicate_common_winners_without_settlement_inference():
    raw = race_archive(
        payout_rows=[
            {
                "馬複組番1": "1",
                "馬複組番2": "2",
                "馬複払戻金（円）": "650",
                "単勝組番": "1",
                "単勝払戻金（円）": "200",
            },
            {
                "馬複組番1": "3",
                "馬複組番2": "1",
                "馬複払戻金（円）": "720",
                "単勝組番": "1",
                "単勝払戻金（円）": "200",
            },
        ]
    )
    race = parse_race_bundle(raw)["races"][f.RACE]
    assert len(race["payout_tickets"]) == 3
    assert {x["selection"] for x in race["payout_tickets"] if x["market"] == "quinella"} == {"1-2", "1-3"}
    assert race["refund_coverage"] == "UNKNOWN" and not race["final"]
    with pytest.raises(ValueError, match="CONFLICTING_PAYOUT"):
        parse_race_bundle(
            race_archive(
                payout_rows=[
                    {"単勝組番": "1", "単勝払戻金（円）": "200"},
                    {"単勝組番": "1", "単勝払戻金（円）": "210"},
                ]
            )
        )
    with pytest.raises(ValueError, match="UNQUALIFIED"):
        parse_race_bundle(race_archive(payout_rows=[{"単勝組番": "返還", "単勝払戻金（円）": "100"}]))


def test_coverage_uses_entries_only_and_detects_missing_and_extra():
    odds = parse_odds(f.archive(), {})
    bundle = parse_race_bundle(race_archive())
    del odds[f.RACE]["markets"]["quinella"]["quotes"]["1-2"]
    odds[f.RACE]["markets"]["quinella"]["quotes"]["1-9"] = {"display_status": "UNKNOWN"}
    coverage = completeness(odds, bundle)[f.RACE]
    assert coverage["markets"]["quinella"]["missing"] == ["1-2"]
    assert coverage["markets"]["quinella"]["unexpected"] == ["1-9"]
    assert coverage["markets"]["exacta"]["expected_rows"] == 12
    assert not coverage["paper_eligible"]
    assert "wide" in coverage["absent_markets"]
    assert not parse_race_bundle(race_archive(horse_count=3))["races"][f.RACE]["entry_count_matches"]


def test_manual_zip_is_asset_not_observation_and_quarantine_retains_raw(store):
    adapter = RealData(store)
    raw = f.archive()
    report = adapter.inspect(raw, "download.zip", "DAILY_SNAPSHOT", "utf-8-sig")
    assert report["status"] == "PARSED_UNQUALIFIED"
    assert report["collector_received_at"] is None and not report["paper_eligible"]
    assert store.metrics()["observations"] == 0
    assert store.asof(f.RACE, ["win"], f.at(3))["reason"] == "DATA_MISSING"
    assert adapter.inspect(raw, "download.zip", "DAILY_SNAPSHOT", "utf-8-sig") == report
    bad = adapter.inspect(b"not a zip", "bad.zip", "DAILY_SNAPSHOT", "cp932")
    assert bad["status"] == "QUARANTINED"
    assert store.read_body(bad["raw_hash"], "raw") == b"not a zip"
    with pytest.raises(ValueError, match="FINAL_ONLY"):
        adapter.inspect(raw, "202609_1790580000_odds.zip", "DAILY_SNAPSHOT", "utf-8-sig")
    final = adapter.inspect(raw, "202609_1790580000_odds.zip", "FINAL_ONLY", "utf-8-sig")
    assert final["recipe"]["dataset_kind"] == "FINAL_ONLY"


def test_import_preserves_same_bytes_as_distinct_observations_and_redelivery(store):
    adapter = RealData(store)
    store.clock = lambda: f.at(30)
    adapter.import_capture(manifest(), f.archive(), "utf-8-sig")
    adapter.import_capture(manifest(2), f.archive(), "utf-8-sig")
    before = store.metrics()
    store.clock = lambda: f.at(40)
    adapter.import_capture(manifest(2), f.archive(), "utf-8-sig")
    assert store.metrics()["observations"] == before["observations"] == 2
    assert store.metrics()["raw_objects"] == 1
    assert store.asof(f.RACE, ["quinella"], f.at(3))["reason"] == "DATA_MISSING"
    rows = store.history(f.RACE, "quinella")
    assert all(x["available_at"] == stamp(f.at(30)) for x in rows)
    assert rows[0]["received_at"] == stamp(f.at(0, 2))
    assert rows[0]["event"]["collector_raw_saved_at"] == f.at(0, 3)
    assert rows[0]["content"]["source_updated_at"] is None
    assert rows[0]["content"]["state"]["status"] == "UNKNOWN"
    with pytest.raises(ValueError, match="CONFLICT"):
        adapter.import_capture(manifest(2, etag='"changed"'), f.archive(), "utf-8-sig")
    reopened = Store(store.root, clock=lambda: f.at(50))
    assert len(reopened.history(f.RACE, "quinella")) == 2
    assert reopened.asof(f.RACE, ["quinella"], f.at(31))["reason"] == "STALE"
    reopened.close()


def test_304_import_requires_saved_matching_earlier_200(store):
    adapter = RealData(store)
    store.clock = lambda: f.at(30)
    not_modified = manifest(2, http_status=304, validator_sent='"v1"', validator_raw_sha256=sha(f.archive()))
    with pytest.raises(ValueError, match="200_FIRST"):
        adapter.import_capture(not_modified, f.archive(), "utf-8-sig")
    assert store.metrics()["observations"] == 0
    adapter.import_capture(manifest(), f.archive(), "utf-8-sig")
    adapter.import_capture(not_modified, f.archive(), "utf-8-sig")
    assert store.latest(f.RACE, "win")["basis"] == "validator_304"
    assert store.metrics()["observations"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"raw_sha256": "0" * 64},
        {"raw_bytes": 0},
        {"http_status": 503},
        {"event_id": "other-source:1"},
        {"event_id": "nar-daily-odds:1"},
        {"raw_saved_at": f.at(1, 1), "collector_received_at": f.at(2)},
        {"collector_received_at": f.at(50)},
        {"file_timestamp": f.at(0)},
        {"file_name": "202609_1790580000_odds.zip"},
        {"duration_ms": float("nan")},
    ],
)
def test_manifest_rejects_invalid_evidence(change):
    with pytest.raises(ValueError):
        validate_manifest(manifest(**change), f.archive(), stamp(f.at(30)))


def test_import_then_reparse_never_backdates_new_result(store):
    adapter = RealData(store)
    store.clock = lambda: f.at(10)
    parsed = adapter.import_capture(manifest(), f.archive(), "utf-8-sig")
    store.clock = lambda: f.at(30)
    store.reparse(manifest()["event_id"], "revised-v2")
    assert store.asof(f.RACE, ["win"], f.at(5))["reason"] == "DATA_MISSING"
    assert store.asof(f.RACE, ["win"], f.at(11))["markets"]["win"]["parse_id"] == parsed
    assert len(store.history(f.RACE, "win")) == 2


def test_filename_never_substitutes_for_market_or_capture_time():
    assert filename_metadata("unknown.zip")["file_timestamp"] is None
    assert filename_metadata(None)["filename_kind"] is None


def test_cli_no_raw_stdout_and_rejects_ci(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # Unit-level root injection; production root checks exercised separately below.
    monkeypatch.setattr("hr_platform.cli.private_root", lambda _: tmp_path / "private")
    rawfile = tmp_path / "odds.zip"
    rawfile.write_bytes(f.archive())
    assert (
        main(["inspect", "--zip", str(rawfile), "--kind", "DAILY_SNAPSHOT", "--encoding", "utf-8-sig"]) == 0
    )
    output = capsys.readouterr().out
    assert "SYNTHETIC" not in output and 'odds"' not in output
    assert json.loads(output)["status"] == "PARSED_UNQUALIFIED"
    monkeypatch.setenv("CI", "true")
    assert main(["inspect"]) == 2
    assert "DISABLED_IN_CI" in capsys.readouterr().err


def test_private_root_refuses_public_paths_and_symlinks(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("private/\n")
    monkeypatch.chdir(tmp_path)
    assert private_root("private/real") == tmp_path / "private/real"
    with pytest.raises(ValueError, match="PRIVATE_ROOT_REQUIRED"):
        private_root("docs/results")
    (tmp_path / "private").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="SYMLINK"):
        private_root("private/real")


def test_corrupt_schema_is_retained_without_cell_in_console(store):
    raw = race_archive(payout_rows=[{"単勝組番": "PRIVATE_MARKER", "単勝払戻金（円）": "100"}])
    report = RealData(store).inspect(raw, "race.zip", "DAILY_SNAPSHOT", "utf-8-sig")
    assert report["status"] == "QUARANTINED"
    assert "PRIVATE_MARKER" not in json.dumps(report)
    assert store.read_body(report["raw_hash"], "raw") == raw


def test_304_redelivery_does_not_rebind_after_out_of_order_200_import(store):
    adapter = RealData(store)
    store.clock = lambda: f.at(30)
    adapter.import_capture(manifest(0), f.archive(), "utf-8-sig")
    receipt = manifest(4, http_status=304, validator_sent='"v1"', validator_raw_sha256=sha(f.archive()))
    adapter.import_capture(receipt, f.archive(), "utf-8-sig")
    adapter.import_capture(manifest(2), f.archive(), "utf-8-sig")
    store.clock = lambda: f.at(40)
    adapter.import_capture(receipt, f.archive(), "utf-8-sig")
    assert store.metrics()["observations"] == 3
    old = json.loads(
        store.db.execute("SELECT event FROM attempts WHERE id=?", (receipt["event_id"],)).fetchone()[0]
    )
    assert old["validator_attempt"] == manifest(0)["event_id"]


def test_intent_manifest_without_completed_storage_is_rejected(store):
    with pytest.raises(ValueError, match="MANIFEST_TIME"):
        RealData(store).import_capture(manifest(raw_saved_at=None), f.archive(), "utf-8-sig")
    assert store.metrics()["observations"] == 0


@pytest.mark.parametrize(
    "child", ["raw", "normalized", "receipts", "reports", "index.sqlite", "index.sqlite-wal"]
)
def test_internal_store_symlink_never_writes_public_tree(tmp_path, child):
    root = tmp_path / "private"
    root.mkdir()
    public = tmp_path / "docs"
    public.mkdir()
    target = public / "unexpected.sqlite" if child.startswith("index") else public
    (root / child).symlink_to(target)
    with pytest.raises(ValueError, match="STORE_SYMLINK"):
        Store(root)
    assert not list(public.iterdir())


def test_body_symlink_created_after_open_is_rejected_before_write(store, tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (store.root / "reports").symlink_to(public)
    with pytest.raises(ValueError, match="STORE_SYMLINK"):
        store.body(b"PRIVATE_MARKER", "reports")
    assert not list(public.iterdir())


def test_hash_named_symlink_and_database_hardlink_are_rejected(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    with pytest.raises(ValueError, match="STORE_PATH"):
        Store(root).body(b"payload", "../public")
    database = tmp_path / "shared.sqlite"
    database.touch()
    (root / "index.sqlite").unlink()
    __import__("os").link(database, root / "index.sqlite")
    with pytest.raises(ValueError, match="STORE_HARDLINK"):
        Store(root)


def test_receipt_id_cannot_adopt_existing_synthetic_or_other_attempt(store):
    receipt = manifest()
    store.ingest(f.event(receipt["event_id"], 0), f.archive())
    adapter = RealData(store)
    store.clock = lambda: f.at(30)
    with pytest.raises(ValueError, match="CAPTURE_IMPORT_CONFLICT"):
        adapter.import_capture(receipt, f.archive(), "utf-8-sig")
    assert store.db.execute("SELECT count(*) FROM capture_imports").fetchone()[0] == 0


def test_popularity_alone_does_not_prove_results_or_pre_race():
    race = parse_race_bundle(race_archive(popularity=True))["races"][f.RACE]
    assert not race["result_present"]
    assert race["status"] == "UNKNOWN" and race["pre_race_evidence"] is None
    assert all(h["result_fields"]["人気"] for h in race["horses"].values())
