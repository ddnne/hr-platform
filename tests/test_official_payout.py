import json
import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical
from hr_platform.paper import decide
from hr_platform.official_payout import HEADERS, PayoutEvidence, parse_payout_page
from hr_platform.race_state import StateEvidence
from test_race_state import receipt as state_receipt


def page(status="除外", amount=650, *, corners=True):
    headers = HEADERS if corners else [h for h in HEADERS if h != "コーナー通過順"]
    header = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    order = [(1, "1"), (2, status), (3, "2"), (4, "3")]
    rows = "".join(
        "<tr>" + "".join(f"<td>{v}</td>" for v in [rank, "1", str(h)] + ["SYNTHETIC"] * (len(headers) - 3)) + "</tr>"
        for h, rank in order
    )
    return (
        '<a id="RaceList" href="../TodayRaceInfo/RaceList?k_babaCode=19&amp;k_raceDate=2000%2F01%2F01&amp;k_raceNo=1">出馬表</a>'
        "<h4>2000年1月1日（土） SYNTHETIC 第1競走 競走成績</h4>"
        f'<section class="gradeTable"><table>{header}{rows}</table></section>'
        '<section class="newRefundTable"><table>'
        f'<tr><td class="title">馬連複</td><td class="a">1-3</td><td class="refundMoney">{amount}円</td></tr>'
        '<tr><td class="title">三連複</td><td class="d">1-3-4</td><td class="refundMoney">900円</td></tr>'
        "</table></section>"
    ).encode()


def receipt(raw, minute=15):
    r = state_receipt(raw, minute)
    r["url"] = r["url"].replace("OddsTanFuku", "RaceMarkTable")
    return r


def test_missing_corner_column_preserves_payouts_and_requires_matching_rows():
    assert parse_payout_page(page(corners=False), f.RACE) == parse_payout_page(page(), f.RACE)
    # A missing header alone must not silently shift mismatched result rows.
    malformed = page().decode().replace("<th>コーナー通過順</th>", "").encode()
    with pytest.raises(ValueError, match="PAYOUT_GRADE_ROW"):
        parse_payout_page(malformed, f.RACE)


@pytest.mark.parametrize("status,refunds", [("除外", 6), ("中止", 0), ("取消", 0)])
def test_refund_rule_distinguishes_exclusion_nonfinish_and_cancellation(status, refunds):
    result = parse_payout_page(page(status), f.RACE)
    assert result["status"] == "PAYOUT_QUALIFIED"
    assert result["final"] and result["complete_markets"] == ["quinella", "trio"]
    assert not result["paper_eligible"] and result["source_updated_at"] is None
    tickets = result["tickets"]
    assert sum("refund_per_100" in r for r in tickets) == refunds
    assert all(
        r["refund_per_100"] == 100 and "2" in r["selection"].split("-")
        for r in tickets
        if "refund_per_100" in r
    )
    assert {r["market"] for r in tickets} == {"quinella", "trio"}


@pytest.mark.parametrize("status,payout,refund", [("除外", 0, 100), ("中止", 0, 0), ("4", 0, 0)])
def test_existing_decision_settlement_and_replay(collected, config, status, payout, refund):
    decision = next(d for d in decide(collected, f.RACE, f.schedule(), config) if d["model"] == "reference")
    original = canonical(decision)
    collected.clock = lambda: f.at(21)
    raw = page(status)
    evidence = PayoutEvidence(collected)
    report = evidence.ingest(receipt(raw), raw, f.RACE)
    settled = evidence.settle_decision(decision["id"], report["id"])
    assert (
        settled["status"] == "SETTLED" and settled["refund_yen"] == refund and settled["payout_yen"] == payout
    )
    assert settled["profit_yen"] == payout + refund - 100
    assert evidence.settle_decision(decision["id"], report["id"]) == settled
    assert (
        canonical(
            json.loads(
                collected.db.execute("SELECT body FROM decisions WHERE id=?", (decision["id"],)).fetchone()[0]
            )
        )
        == original
    )
    assert collected.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 1


@pytest.mark.parametrize(
    "target,selection,paid", [("quinella", "1-3", 650), ("trio", "1-3-4", 900), ("trio", "1-2-4", 0)]
)
def test_published_amounts_and_trio_refund(collected, config, target, selection, paid):
    # Explicitly synthetic decision fixture; no historical real purchases.
    decision = next(d for d in decide(collected, f.RACE, f.schedule(), config) if d["model"] == "reference")
    decision.update(target=target, selection=selection)
    with collected.db:
        collected.db.execute(
            "UPDATE decisions SET body=? WHERE id=?", (canonical(decision).decode(), decision["id"])
        )
    collected.clock = lambda: f.at(21)
    e = PayoutEvidence(collected)
    report = e.ingest(receipt(page()), page(), f.RACE)
    result = e.settle_decision(decision["id"], report["id"])
    assert result["payout_yen"] == paid
    assert result["refund_yen"] == (100 if "2" in selection.split("-") else 0)


def test_cancelled_before_sales_never_becomes_refund_or_losing_ticket(collected, config):
    d = next(d for d in decide(collected, f.RACE, f.schedule(), config) if d["model"] == "reference")
    collected.clock = lambda: f.at(21)
    e = PayoutEvidence(collected)
    raw = page("取消")
    report = e.ingest(receipt(raw), raw, f.RACE)
    with pytest.raises(ValueError, match="NOT_SOLD"):
        e.settle_decision(d["id"], report["id"])
    assert collected.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_raw_history_availability_replay_reparse_and_separate_state(store):
    store.clock = lambda: f.at(21)
    e = PayoutEvidence(store)
    raw = page()
    first = e.ingest(receipt(raw), raw, f.RACE)
    assert e.ingest(receipt(raw), raw, f.RACE) == first
    assert e.asof(f.RACE, f.at(20))["evidence"] is None
    assert e.asof(f.RACE, f.at(21))["evidence"] == first
    store.clock = lambda: f.at(25)
    second = e.ingest(receipt(raw, 23), raw, f.RACE)
    assert first["raw_hash"] == second["raw_hash"] and first["observation_id"] != second["observation_id"]
    store.clock = lambda: f.at(30)
    reparsed = e.ingest(receipt(raw), raw, f.RACE, version="synthetic-v2")
    assert reparsed["available_at"] > first["available_at"]
    assert e.asof(f.RACE, f.at(30))["evidence"] == second
    assert e.history(f.RACE, f.at(21))["history"] == [first]
    assert len(e.history(f.RACE, f.at(30))["history"]) == 3
    assert StateEvidence(store).history(f.RACE, f.at(30))["history"] == []
    assert store.read_body(first["raw_hash"], "raw") == raw
    assert store.metrics()["observations"] == 0
    for table in ["decisions", "settlements"]:
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "before,after",
    [
        ("SYNTHETIC 第", "OTHER 第"),
        ("第1競走", "第2競走"),
        ("1月1日", "1月2日"),
        ("馬番", "不明"),
        ("除外", "不成"),
        ("除外", "1"),
        ("除外", ""),
        ("<td>4</td>", "<td>5</td>"),
        ("<td>3</td><td>1</td>", "<td>4</td><td>1</td>"),
        ("650円", "特払い"),
        ("1-3</td>", "1-2</td>"),
        ("1-3-4</td>", "1-2-3</td>"),
        ("<table>", "<table hidden>"),
        ("除外", '<span style="display:none">除外</span>'),
        ('<section class="gradeTable">', '<div hidden><section class="gradeTable">'),
        ('id="RaceList"', 'hidden id="RaceList"'),
        ("<h4>", "<h4 hidden>"),
        ("</table></section>", "<tr><td>unfinished</table></section>"),
        ("gradeTable", "unknown"),
    ],
)
def test_unsupported_results_and_conflicting_or_hidden_evidence_quarantine(store, before, after):
    store.clock = lambda: f.at(21)
    raw = page().decode().replace(before, after).encode()
    e = PayoutEvidence(store)
    report = e.ingest(receipt(raw), raw, f.RACE)
    assert report["status"] == "QUARANTINED" and "complete_markets" not in report
    assert store.read_body(report["raw_hash"], "raw") == raw
    with pytest.raises(ValueError, match="UNQUALIFIED"):
        e.settle_decision("missing", report["id"])


def test_wrong_venue_receipt_cannot_qualify_and_wrong_path_cannot_import(store):
    store.clock = lambda: f.at(21)
    raw = page()
    r = receipt(raw)
    r["url"] = r["url"].replace("babaCode=19", "babaCode=20")
    assert PayoutEvidence(store).ingest(r, raw, f.RACE)["status"] == "QUARANTINED"
    with pytest.raises(ValueError, match="RECEIPT_URL"):
        PayoutEvidence(store).ingest(state_receipt(raw), raw, f.RACE)


def test_cli_import_reports_only_private_path_and_unknown_decision_cannot_create_bet(
    tmp_path, monkeypatch, capsys
):
    from hr_platform import cli

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "private_root", lambda _: tmp_path / "store")
    raw, rec = tmp_path / "result.html", tmp_path / "receipt.json"
    raw.write_bytes(page())
    rec.write_bytes(canonical(receipt(page())))
    assert cli.main(["import-payout", "--html", str(raw), "--receipt", str(rec), "--race", f.RACE]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {"status", "private_report"} and summary["status"] == "PAYOUT_QUALIFIED"
    from pathlib import Path

    report = json.loads(Path(summary["private_report"]).read_text())
    assert cli.main(["settle-payout", "--decision", "missing", "--evidence", report["id"]]) == 2
    assert "650" not in capsys.readouterr().err
