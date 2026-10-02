import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import pytest
from hr_platform import fixtures as f
from hr_platform.cli import main
from hr_platform.collector_log import CaptureLog, FIELDS, FORMAT, SOURCE, validate_export
from hr_platform.common import canonical, instant, stamp
from hr_platform.realdata import RealData
from hr_platform.store import Store
from test_realdata import manifest


def record(minute=0, status="RAW_STORED", **overrides):
    row = dict.fromkeys(FIELDS)
    row.update(
        event_id=f"{SOURCE}:{int(instant(f.at(minute)).timestamp() * 1000)}",
        scheduled_capture_at=f.at(minute),
        status=status,
    )
    if status == "RAW_STORED":
        row.update(manifest(minute))
    elif status != "WAIT_OR_BLOCKED":
        row.update(fetch_started_at=f.at(minute), duration_ms=20000)
    if status in {"FAILED", "STORAGE_ERROR"}:
        row["error_code"] = "FETCH_TIMEOUT" if status == "FAILED" else "STORAGE_ERROR"
    row.update(overrides)
    return row


def envelope(*rows, read_minute=20):
    return {
        "format": FORMAT,
        "source": SOURCE,
        "read_started_at": f.at(read_minute),
        "read_completed_at": f.at(read_minute, 1),
        "range_start": f.at(0),
        "range_end": f.at(10),
        "rows": list(rows),
    }


def test_log_retains_failures_nulls_and_grid_without_inventing_schedule(store):
    log = CaptureLog(store)
    store.clock = lambda: f.at(30)
    data = envelope(record(), record(2), record(4, "FAILED"), record(6, "WAIT_OR_BLOCKED"))
    log.ingest(data)
    assert store.metrics()["observations"] == store.metrics()["attempts"] == 0
    assert log.history(f.at(0), f.at(10), f.at(29))["events"] == []
    result = log.history(f.at(0), f.at(10), f.at(30))
    assert [s["status"] for s in result["audit_slots"]] == [
        "RAW_STORED",
        "RAW_STORED",
        "FAILED",
        "WAIT_OR_BLOCKED",
        "NO_CAPTURE_RECORD",
    ]
    assert result["scheduled_coverage"] is None
    assert result["events"][2]["current"]["capture"]["collector_received_at"] is None
    assert result["events"][3]["current"]["capture"]["fetch_started_at"] is None
    store.clock = lambda: f.at(40)
    second = log.ingest(data)
    assert second["available_at"] == stamp(f.at(30))
    assert len(log.history(f.at(0), f.at(10), f.at(40))["events"][0]["revisions"]) == 1
    reopened = Store(store.root)
    assert len(CaptureLog(reopened).history(f.at(0), f.at(10), f.at(40))["events"]) == 4
    reopened.close()


def test_repair_and_late_export_never_overwrite_history_or_create_market_values(store):
    log = CaptureLog(store)
    store.clock = lambda: f.at(30)
    log.ingest(envelope(record(0, "STORAGE_ERROR")))
    store.clock = lambda: f.at(40)
    log.ingest(envelope(record(), read_minute=35))
    store.clock = lambda: f.at(50)
    log.ingest(envelope(record(0, "PENDING"), read_minute=15))
    before = log.history(f.at(0), f.at(10), f.at(31))["events"][0]
    after = log.history(f.at(0), f.at(10), f.at(51))["events"][0]
    assert before["current"]["capture"]["status"] == "STORAGE_ERROR"
    assert after["current"]["capture"]["status"] == "RAW_STORED"
    assert len(after["revisions"]) == 3 and not after["local_parse_available"]
    assert store.asof(f.RACE, ["win"], f.at(51))["reason"] == "DATA_MISSING"
    RealData(store).import_capture(manifest(), f.archive(), "utf-8-sig")
    assert log.history(f.at(0), f.at(10), f.at(51))["events"][0]["local_parse_available"]
    assert not log.history(f.at(0), f.at(10), f.at(49))["events"][0]["local_parse_available"]


@pytest.mark.parametrize(
    "change",
    [
        {"source": "other"},
        {"format": "unknown"},
        {"read_completed_at": f.at(100)},
        {"range_end": f.at(2000)},
        {"rows": [record(), record()]},
        {"rows": [record(http_status=403)]},
        {"rows": [record(raw_saved_at=None)]},
        {"rows": [record(0, "WAIT_OR_BLOCKED", collector_received_at=f.at(0))]},
        {"rows": [record(0, "FAILED", duration_ms=float("nan"))]},
        {"rows": [record(scheduled_capture_at=f.at(1))]},
        {"rows": [record(http_status=304)]},
        {"rows": [record(raw_sha256="wrong")]},
    ],
)
def test_invalid_log_has_no_partial_effect(store, change):
    store.clock = lambda: f.at(30)
    log = CaptureLog(store)
    with pytest.raises(ValueError):
        log.ingest({**envelope(record()), **change})
    assert store.db.execute("SELECT count(*) FROM collector_exports").fetchone()[0] == 0
    assert store.metrics()["observations"] == 0


def test_same_event_id_from_synthetic_attempt_is_not_linked(store):
    store.ingest(f.event(manifest()["event_id"], 0), f.archive())
    store.clock = lambda: f.at(30)
    log = CaptureLog(store)
    log.ingest(envelope(record()))
    event = log.history(f.at(0), f.at(10), f.at(31))["events"][0]
    assert not event["local_parse_available"] and event["local_parse"] is None


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_collector_log", "scripts/export_collector_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_overlapping_reads_keep_order_uncertain_until_later_nonoverlapping_read(store):
    log = CaptureLog(store)
    store.clock = lambda: f.at(50)
    log.ingest(envelope(record(), read_minute=30))
    store.clock = lambda: f.at(51)
    log.ingest({**envelope(record(0, "PENDING")), "read_completed_at": f.at(40)})
    report = log.history(f.at(0), f.at(10), f.at(52))
    event = report["events"][0]
    assert event["current"] is None and event["order_uncertain"]
    assert len(event["current_candidates"]) == 2
    assert report["audit_slots"][0]["status"] == "ORDER_UNCERTAIN"
    assert not event["local_parse_available"]
    assert log.history(f.at(0), f.at(10), f.at(50))["events"][0]["current_status"] == "RAW_STORED"
    store.clock = lambda: f.at(60)
    log.ingest(envelope(record(), read_minute=55))
    event = log.history(f.at(0), f.at(10), f.at(60))["events"][0]
    assert not event["order_uncertain"] and event["current_status"] == "RAW_STORED"
    assert len(event["revisions"]) == 3


def test_identical_overlapping_statuses_are_not_falsely_conflicting(store):
    log = CaptureLog(store)
    store.clock = lambda: f.at(50)
    log.ingest(envelope(record(), read_minute=30))
    log.ingest({**envelope(record()), "read_completed_at": f.at(40)})
    event = log.history(f.at(0), f.at(10), f.at(51))["events"][0]
    assert not event["order_uncertain"] and len(event["revisions"]) == 2


def test_unaligned_audit_window_does_not_hide_records_between_grid_points(store):
    log = CaptureLog(store)
    store.clock = lambda: f.at(50)
    log.ingest(envelope(record(2), record(4, "FAILED")))
    slots = log.history(f.at(1), f.at(6), f.at(51))["audit_slots"]
    assert [s["status"] for s in slots] == ["RAW_STORED", "FAILED", "NO_CAPTURE_RECORD"]
    assert slots[-1]["until"] == stamp(f.at(6))


def test_exporter_selects_full_range_handles_z_timestamps_and_keeps_output_private(store):
    module = load_exporter()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        Path("migrations/0001_capture.sql").read_text()
        + Path("migrations/0002_processing_metrics.sql").read_text()
    )
    rows = [record(), record(2, "FAILED"), record(8, "WAIT_OR_BLOCKED"), record(10),
            {**record(4), 'status': 'SYNTHETIC_FIXTURE'},
            {**record(6), 'event_id': 'nar-mac-import:' + 'a' * 64, 'status': 'IMPORTED_RAW_STORED'},
            {**record(4), 'event_id': 'sports:boat:123:' + 'b' * 64},
            {**record(6), 'event_id': 'nar-daily-race:123'},
            {**record(8), 'event_id': 'nar-monthly-odds:123'}]
    for r in rows:
        r["scheduled_capture_at"] = (
            instant(r["scheduled_capture_at"]).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        db.execute(
            f"INSERT INTO captures({','.join(FIELDS)}) VALUES({','.join('?' for _ in FIELDS)})",
            [r[k] for k in FIELDS],
        )
    store.clock = lambda: f.at(30)

    def run(args, **kwargs):
        assert args[:4] == ["node_modules/.bin/wrangler", "d1", "execute", module.DATABASE]
        assert "--remote" in args and kwargs["stdin"] == subprocess.DEVNULL
        sql = args[args.index("--command") + 1]
        assert sql.startswith("SELECT ")
        selected = [dict(r) for r in db.execute(sql)]
        return subprocess.CompletedProcess(
            args, 0, canonical([{"success": True, "results": selected}]), b"PRIVATE_DIAGNOSTIC"
        )

    result = module.export(store, f.at(0), f.at(10), runner=run)
    data = json.loads(Path(result["private_export"]).read_text())
    assert len(data["rows"]) == 3
    validate_export(data, f.at(31))
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(result)
    assert data["rows"][1]["collector_received_at"] is None


def test_exporter_refuses_truncated_or_failed_results(store):
    module = load_exporter()
    store.clock = lambda: f.at(30)
    for response in ([{"success": False, "results": []}], [{"success": True, "results": [record()] * 10001}]):
        with pytest.raises(ValueError):
            module.export(
                store,
                f.at(0),
                f.at(10),
                runner=lambda *a, **k: subprocess.CompletedProcess([], 0, canonical(response), b""),
            )


def test_log_cli_and_remote_export_disabled_in_ci_and_no_payload_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr("hr_platform.cli.private_root", lambda _: tmp_path / "private")
    path = tmp_path / "log.json"
    path.write_bytes(canonical(envelope(record(0, "FAILED", error_code="PRIVATE_MARKER"))))
    assert main(["import-log", "--file", str(path)]) == 0
    assert "PRIVATE_MARKER" not in capsys.readouterr().out
    monkeypatch.setenv("CI", "true")
    assert main(["import-log"]) == 2
    assert load_exporter().main([]) == 2
    assert "DISABLED_IN_CI" in capsys.readouterr().err
