import io
import json
import pytest
from hr_platform import fixtures as f
from hr_platform.common import stamp
from hr_platform.sampling import Samples, ZIP_URLS, FORMAT, validate_plan, after, NoRedirect, open_response
from hr_platform.store import Store
from test_race_state import page, receipt as state_receipt
from test_realdata import race_archive


@pytest.fixture(autouse=True)
def private_test_environment(monkeypatch):
    # The injected opener below never uses a network. Real entry points are
    # separately tested with CI enabled and refuse before making any request.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def plan(kinds=("odds", "odds"), minutes=(4, 8)):
    items = []
    for n, (kind, minute) in enumerate(zip(kinds, minutes)):
        url = ZIP_URLS[kind] if kind in ZIP_URLS else state_receipt(page())["url"]
        if kind == "payout":
            url = url.replace("OddsTanFuku", "RaceMarkTable")
        items.append({"id": f"sample-{n}", "kind": kind, "url": url,
                      "scope": "20000101" if kind in ZIP_URLS else f.RACE,
                      "at": f.at(minute), "until": f.at(minute, 90)})
    return {"format": FORMAT, "requests": items}


class Response(io.BytesIO):
    def __init__(self, raw=None, status=200, *, kind="odds", headers=None, broken=False, close_error=False):
        super().__init__(f.archive() if raw is None else raw)
        self.status = status
        self.headers = {"Content-Type": "application/zip" if kind in ZIP_URLS else "text/html",
                        "Content-Disposition": f'attachment; filename="20000101_0946706400_{kind}.zip"',
                        "Set-Cookie": "SYNTHETIC_PRIVATE_COOKIE", **(headers or {})}
        self.broken, self.close_error = broken, close_error

    def read(self, n):
        if self.broken:
            raise OSError("synthetic-stream-failure")
        return super().read(n)

    def close(self):
        super().close()
        if self.close_error:
            raise OSError("synthetic-close-failure")


def test_capture_replay_same_body_future_change_history_and_reopen(store):
    calls = []
    raw = f.archive()
    def opener(item):
        calls.append(item)
        return Response(raw)
    samples = Samples(store, opener)
    p = plan()
    assert samples.capture(p, "sample-0")["status"] == "WAIT"
    store.clock = lambda: f.at(4, 10)
    first = samples.capture(p, "sample-0")
    before = store.asof(f.RACE, ["quinella"], f.at(5))
    assert first["status"] == "PARSED" and len(calls) == 1
    assert samples.capture(p, "sample-0") == first
    receipt = json.loads(store.read_body(first["receipt_hash"], "receipts"))
    assert receipt["source_updated_at"] is None and "Set-Cookie" not in receipt["headers"]
    assert receipt["duration_ms"] >= 0
    store.clock = lambda: f.at(8, 10)
    second = samples.capture(p, "sample-1")
    assert len(calls) == 2
    assert first["attempt_id"] != second["attempt_id"]
    assert first["parse"]["available_at"] < second["parse"]["available_at"]
    assert store.metrics()["observations"] == 2 and store.metrics()["raw_objects"] == 1
    assert len(store.history(f.RACE, "quinella")) == 2
    assert store.asof(f.RACE, ["quinella"], f.at(5)) == before
    reopened = Store(store.root, clock=lambda: f.at(9))
    assert Samples(reopened, opener).capture(p, "sample-1") == second
    assert len(calls) == 2
    reopened.close()


def monthly_plan(minute=4):
    return {"format": FORMAT, "requests": [{
        "id": "monthly", "kind": "monthly_odds", "scope": "200001",
        "url": ZIP_URLS["odds"].replace("type=daily", "type=monthly&k_year=2000&k_month=1"),
        "at": f.at(minute), "until": f.at(minute, 90)}]}


def test_monthly_inspection_is_separate_replayable_and_shares_limits(store):
    calls = []
    def opener(item):
        calls.append(item)
        filename = "200001_0946706400_odds.zip" if item["kind"] == "monthly_odds" else "20000101_0946706400_odds.zip"
        return Response(headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    samples = Samples(store, opener)
    monthly = monthly_plan()
    samples.register(monthly)
    daily = plan(("odds",), (5,))
    samples.register(daily)
    store.clock = lambda: f.at(4)
    captured = samples.capture(monthly, "monthly")
    assert captured["status"] == "INSPECTED"
    assert captured["inspection"]["recipe"]["dataset_kind"] == "FINAL_ONLY"
    assert not captured["inspection"]["paper_eligible"]
    assert captured["inspection"]["source_updated_at"] is None
    assert samples.capture(monthly, "monthly") == captured
    assert store.metrics()["observations"] == 0
    assert not store.history(f.RACE, "quinella")
    store.clock = lambda: f.at(5)
    assert samples.capture(daily, "sample-0")["status"] == "WAIT"
    store.clock = lambda: f.at(6)
    assert samples.capture(daily, "sample-0")["status"] == "PARSED"
    later = monthly_plan(8)
    samples.register(later)
    store.clock = lambda: f.at(8)
    assert samples.capture(later, "monthly")["next_at"] == stamp(f.at(1445, 30))
    store.clock = lambda: f.at(10)
    assert samples.capture(later, "monthly")["status"] == "SAMPLE_WINDOW_EXPIRED"
    tomorrow = monthly_plan(1446)
    samples.register(tomorrow)
    store.clock = lambda: f.at(1446)
    assert samples.capture(tomorrow, "monthly")["status"] == "INSPECTED"
    assert len(calls) == 3 and store.metrics()["observations"] == 1
    assert store.db.execute("SELECT count(*) FROM asset_inspections").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [
    ("scope", "200013"), ("scope", "20000101"), ("scope", "199912"),
    ("url", ZIP_URLS["odds"]), ("url", "https://other.example/monthly"),
])
def test_monthly_scope_and_route_are_explicit(field, value):
    p = monthly_plan()
    p["requests"][0][field] = value
    with pytest.raises(ValueError):
        validate_plan(p)


@pytest.mark.parametrize("status,filename,expected", [
    (403, "200001_0946706400_odds.zip", "SOURCE_DENIED"),
    (200, "199912_0946706400_odds.zip", "BODY_UNQUALIFIED"),
    (200, "20000101_0946706400_odds.zip", "BODY_UNQUALIFIED"),
])
def test_monthly_failure_retains_receipt_and_stops_daily(store, status, filename, expected):
    samples = Samples(store, lambda _: Response(status=status, headers={"Content-Disposition": filename}))
    p = monthly_plan()
    daily = plan(("odds",), (8,))
    samples.register(p)
    samples.register(daily)
    store.clock = lambda: f.at(4)
    result = samples.capture(p, "monthly")
    assert result["status"] == expected
    assert samples.capture(p, "monthly") == result
    store.clock = lambda: f.at(8)
    assert samples.capture(daily, "sample-0")["status"] == "SOURCE_STOPPED"
    assert store.metrics()["observations"] == 0


@pytest.mark.parametrize("status,body,headers,expected", [
    (403, b"denied", {}, "SOURCE_DENIED"), (404, b"missing", {}, "SOURCE_DENIED"),
    (302, b"redirect", {"Location": "https://other.example/"}, "SOURCE_DENIED"),
    (200, b"CAPTCHA", {}, "SOURCE_DENIED"),
    (200, b"valid-looking", {"cf-mitigated": "challenge"}, "SOURCE_DENIED"),
    (200, b"html error", {}, "BODY_UNQUALIFIED"),
])
def test_refusals_stop_all_types_and_new_plans(store, status, body, headers, expected):
    calls = []
    def opener(item):
        calls.append(item)
        return Response(body, status, headers=headers, close_error=True)
    s = Samples(store, opener)
    p = plan(("odds", "state"))
    s.register(p)
    store.clock = lambda: f.at(4)
    result = s.capture(p, "sample-0")
    assert result["status"] == expected
    assert s.capture(p, "sample-0") == result
    store.clock = lambda: f.at(8)
    assert s.capture(p, "sample-1")["status"] == "SOURCE_STOPPED"
    other = plan(("race",), (10,))
    s.register(other)
    store.clock = lambda: f.at(10)
    assert s.capture(other, "sample-0")["status"] == "SOURCE_STOPPED"
    assert len(calls) == 1 and store.metrics()["observations"] == 0


@pytest.mark.parametrize("retry", ["600", "Sat, 01 Jan 2000 05:14:00 GMT"])
def test_retry_after_waits_across_types_and_replay_then_future_slot_can_resume(store, retry):
    responses = [Response(b"rate limited", 429, headers={"Retry-After": retry}), Response()]
    s = Samples(store, lambda _: responses.pop(0))
    p = plan(("odds", "race", "odds"), (4, 8, 16))
    s.register(p)
    store.clock = lambda: f.at(4)
    limited = s.capture(p, "sample-0")
    assert limited["status"] == "RATE_LIMITED"
    assert s.capture(p, "sample-0") == limited
    store.clock = lambda: f.at(8)
    assert s.capture(p, "sample-1")["status"] == "WAIT"
    assert len(responses) == 1
    store.clock = lambda: f.at(16)
    assert s.capture(p, "sample-2")["status"] == "PARSED"
    assert store.metrics()["observations"] == 1
    assert store.gaps(f.at(16))  # Waiting did not manufacture an earlier capture.


@pytest.mark.parametrize("status,headers", [(403, {}), (429, {"Retry-After": "600"})])
def test_error_body_and_cleanup_failures_cannot_undo_stop_or_wait(store, status, headers):
    p = plan()
    s = Samples(store, lambda _: Response(status=status, headers=headers, broken=True, close_error=True))
    s.register(p)
    store.clock = lambda: f.at(4)
    assert s.capture(p, "sample-0")["status"] == "REQUEST_OR_STORAGE_ERROR"
    control = store.db.execute("SELECT * FROM sample_control").fetchone()
    assert control["stopped"] == 1
    if status == 429:
        assert control["next_at"] >= stamp(f.at(14))


def test_spacing_and_incomplete_reservation_are_shared_by_all_types(store):
    calls = []
    def opener(item):
        calls.append(item)
        return Response()
    s = Samples(store, opener)
    p = plan(("odds", "state"), (4, 6))
    s.register(p)
    store.clock = lambda: f.at(5)
    s.capture(p, "sample-0")
    store.clock = lambda: f.at(6)
    assert s.capture(p, "sample-1")["status"] == "WAIT"
    assert len(calls) == 1
    with store.db:
        store.db.execute("INSERT INTO sample_requests VALUES('unknown','plan','{}',NULL)")
    store.clock = lambda: f.at(7)
    assert s.capture(p, "sample-1")["status"] == "INCOMPLETE_ATTEMPT_STOP"
    assert s.capture(p, "sample-0")["status"] == "PARSED"  # Readback is still allowed.
    assert len(calls) == 1


def test_concurrent_entry_cannot_issue_second_request_while_first_is_open(store):
    p = plan()
    s = Samples(store)
    s.register(p)
    other = plan(("odds",), (4,))
    other["requests"][0]["id"] = "different-plan"
    s.register(other)
    calls = []
    def opener(_):
        second = Store(store.root, clock=lambda: f.at(4))
        try:
            nested = Samples(second, lambda _: pytest.fail("second HTTP request"))
            calls.append(nested.capture(other, "different-plan")["status"])
        finally:
            second.close()
        return Response()
    s.opener = opener
    store.clock = lambda: f.at(4)
    assert s.capture(p, "sample-0")["status"] == "PARSED"
    assert calls == ["INCOMPLETE_ATTEMPT_STOP"]


def test_expired_window_and_storage_delay_never_fetch(store):
    p = plan()
    s = Samples(store, lambda _: pytest.fail("expired HTTP request"))
    s.register(p)
    clock = [f.at(4)]
    store.clock = lambda: clock[0]
    def delay():
        clock[0] = f.at(6)
        return 1
    store.db.create_function("delay_reservation", 0, delay)
    store.db.executescript("""CREATE TRIGGER delay_sample AFTER INSERT ON sample_requests
                           BEGIN SELECT delay_reservation(); END;""")
    assert s.capture(p, "sample-0")["status"] == "SAMPLE_WINDOW_EXPIRED"
    clock[0] = f.at(12)
    assert s.capture(p, "sample-1")["status"] == "SAMPLE_WINDOW_EXPIRED"
    assert len(store.gaps(f.at(12))) == 2


def test_race_and_state_use_existing_timeline_and_parser_failure_keeps_collection(store):
    responses = [Response(race_archive(), kind="race"), Response(b"malformed", kind="state"),
                 Response(page(stage="14:08現在", change=""), kind="state")]
    s = Samples(store, lambda _: responses.pop(0))
    p = plan(("race", "state", "state"), (4, 8, 12))
    s.register(p)
    store.clock = lambda: f.at(4)
    assert s.capture(p, "sample-0")["status"] == "PARSED"
    store.clock = lambda: f.at(8)
    assert s.capture(p, "sample-1")["status"] == "QUARANTINED"
    store.clock = lambda: f.at(12)
    state = s.capture(p, "sample-2")
    assert state["status"] == "PARSED" and state["parse"]["source_updated_at"] is None
    assert store.db.execute("SELECT count(*) FROM race_metadata_observations").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM race_state_observations").fetchone()[0] == 2


def test_saved_receipt_repairs_parser_interruption_without_network(store):
    s = Samples(store, lambda _: Response())
    p = plan()
    s.register(p)
    publish = s.publish
    s.publish = lambda *_: (_ for _ in ()).throw(OSError("interrupted parse"))
    store.clock = lambda: f.at(4)
    with pytest.raises(OSError):
        s.capture(p, "sample-0")
    assert store.metrics()["observations"] == 0
    before = store.asof(f.RACE, ["quinella"], f.at(5))
    s.publish = publish
    s.opener = lambda _: pytest.fail("repeated HTTP")
    store.clock = lambda: f.at(6)
    result = s.capture(p, "sample-0")
    assert result["status"] == "PARSED" and result["parse"]["available_at"] == stamp(f.at(6))
    assert store.asof(f.RACE, ["quinella"], f.at(5)) == before
    assert before["reason"] == "DATA_MISSING"


def test_bounded_body_is_marked_partial_and_never_published(store, monkeypatch):
    monkeypatch.setattr("hr_platform.sampling.MAX_COMPRESSED", 128)
    s = Samples(store, lambda _: Response())
    p = plan()
    s.register(p)
    store.clock = lambda: f.at(4)
    result = s.capture(p, "sample-0")
    assert result["status"] == "BODY_LIMIT"
    receipt = json.loads(store.read_body(result["receipt_hash"], "receipts"))
    assert receipt["body_truncated"] and receipt["bytes"] == 129
    assert store.metrics()["observations"] == 0


def test_odds_parse_failure_then_changed_value_does_not_backfill(store):
    responses = [Response(b"PK\x03\x04invalid-zip"), Response(f.archive("nonuniform"))]
    s = Samples(store, lambda _: responses.pop(0))
    p = plan()
    s.register(p)
    store.clock = lambda: f.at(4)
    assert s.capture(p, "sample-0")["status"] == "QUARANTINED"
    before = store.asof(f.RACE, ["quinella"], f.at(5))
    store.clock = lambda: f.at(8)
    assert s.capture(p, "sample-1")["status"] == "PARSED"
    assert store.asof(f.RACE, ["quinella"], f.at(5)) == before
    assert before["reason"] == "DATA_MISSING" and store.metrics()["observations"] == 2


def test_cli_only_prints_private_report_location(tmp_path, monkeypatch, capsys):
    from hr_platform import cli
    import hr_platform.sampling as module
    monkeypatch.setattr(cli, "private_root", lambda _: tmp_path / "store")
    clock = [f.at(2)]
    monkeypatch.setattr(cli, "Store", lambda root: Store(root, clock=lambda: clock[0]))
    original = module.Samples
    monkeypatch.setattr(module.Samples, "for_plan", lambda store, plan: original(store, lambda _: Response()))
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    args = ["collect-sample", "--plan", str(path), "--item", "sample-0"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "WAIT"
    clock[0] = f.at(4)
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert set(json.loads(out)) == {"status", "private_report"}
    assert "SYNTHETIC" not in out and "keiba.go.jp" not in out


@pytest.mark.parametrize("change", ["unknown_url", "monthly", "spacing", "too_many", "duplicate", "late_end"])
def test_plan_validation(change):
    p = plan()
    if change == "unknown_url":
        p["requests"][0]["url"] = "https://other.example/"
    elif change == "monthly":
        p["requests"][0]["url"] = ZIP_URLS["odds"].replace("daily", "monthly")
    elif change == "spacing":
        p["requests"][1]["at"] = f.at(4, 60)
    elif change == "too_many":
        p["requests"] *= 4
    elif change == "duplicate":
        p["requests"][1]["id"] = p["requests"][0]["id"]
    else:
        p["requests"][1]["until"] = f.at(60)
    with pytest.raises(ValueError):
        validate_plan(p)


def test_ci_blocks_before_request_or_plan_registration(store, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    s = Samples(store, lambda _: pytest.fail("CI network"))
    with pytest.raises(ValueError, match="DISABLED_IN_CI"):
        s.capture(plan(), "sample-0")
    assert store.db.execute("SELECT count(*) FROM sample_plans").fetchone()[0] == 0


def test_native_request_profile_has_no_cookies_auth_or_redirects(monkeypatch):
    recorded = []
    class Opener:
        def open(self, request, timeout):
            recorded.append((request.header_items(), timeout))
            return Response()
    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], NoRedirect)
        assert handlers[0].redirect_request(None, None, None, None, None, None) is None
        return Opener()
    monkeypatch.setattr("urllib.request.build_opener", build)
    open_response(plan()["requests"][0]).close()
    headers, timeout = recorded[0]
    assert {k.lower() for k, _ in headers} == {"user-agent", "accept"} and timeout == 30
    assert after(f.at(4), "bad") == stamp(f.at(6))


@pytest.mark.parametrize("kind,status,raw,expected", [
    ("state", 200, b" " * 9000 + b"CAPTCHA", "SOURCE_DENIED"),
    ("odds", 429, b" " * 9000, "BODY_LIMIT"),
])
def test_late_challenge_or_truncated_error_stops_next_request(store, kind, status, raw, expected):
    calls = []
    def opener(_):
        calls.append(1)
        return Response(raw, status, kind=kind)
    s = Samples(store, opener)
    p = plan((kind, "odds"))
    s.register(p)
    store.clock = lambda: f.at(4)
    assert s.capture(p, "sample-0")["status"] == expected
    store.clock = lambda: f.at(8)
    assert s.capture(p, "sample-1")["status"] == "SOURCE_STOPPED"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["304", "403", "429", "timeout", "broken-200"])
def test_failed_http_attempts_remain_distinct_from_missing_capture(store, failure):
    def opener(_):
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        return Response(b"error", int(failure) if failure.isdigit() else 200,
                        broken=failure == "broken-200")
    s = Samples(store, opener)
    p = plan()
    s.register(p)
    store.clock = lambda: f.at(4)
    first = s.capture(p, "sample-0")
    assert s.capture(p, "sample-0") == first
    assert store.metrics()["attempts"] == 1 and store.metrics()["observations"] == 0
    event = json.loads(store.db.execute("SELECT event FROM attempts").fetchone()[0])
    assert (event["collector_received_at"] is None) == (failure in {"timeout", "broken-200"})
    gaps = store.gaps(f.at(12))
    assert gaps[0]["reason"] == "FAILED_OR_NOT_AVAILABLE"
    assert gaps[1]["reason"] == "NOT_EXECUTED"


def test_partial_plan_registration_repaired_without_http(store, monkeypatch):
    s = Samples(store, lambda _: pytest.fail("registration HTTP"))
    p = plan()
    original = store.plan
    monkeypatch.setattr(store, "plan", lambda _: (_ for _ in ()).throw(OSError("interrupted registration")))
    with pytest.raises(OSError):
        s.register(p)
    assert store.db.execute("SELECT count(*) FROM sample_plans").fetchone()[0] == 1
    monkeypatch.setattr(store, "plan", original)
    store.clock = lambda: f.at(3)
    assert s.capture(p, "sample-0")["status"] == "WAIT"
    assert len(store.gaps(f.at(12))) == 2


def test_payout_uses_existing_evidence_parser(store):
    from test_official_payout import page as payout_page
    s = Samples(store, lambda _: Response(payout_page(), kind="payout"))
    p = plan(("payout",), (4,))
    s.register(p)
    store.clock = lambda: f.at(4)
    result = s.capture(p, "sample-0")
    assert result["status"] == "PARSED"
    assert s.capture(p, "sample-0") == result
    assert store.db.execute("SELECT count(*) FROM official_payout_observations").fetchone()[0] == 1
