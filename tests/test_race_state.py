import json
import pytest
from hr_platform import fixtures as f
from hr_platform.common import sha, stamp
from hr_platform.race_state import HEADERS, StateEvidence, parse_state_page
from hr_platform.store import Store


def page(stage="最終", change="競走除外"):
    headers = "".join(f"<th{' colspan="2"' if i == 4 else ''}>{h}</th>" for i, h in enumerate(HEADERS))
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{v}</td>"
            for v in ["1", str(n), "SYNTHETIC", "", "", "", "", "", "", "", "", "", change if n == 2 else ""]
        )
        + "</tr>"
        for n in [1, 2, 3, 4]
    )
    return (
        '<a class="cNaviBtn live" href="/KeibaWeb/TodayRaceInfo/OddsTanFuku?k_raceDate=2000%2F01%2F01&amp;k_raceNo=1&amp;k_babaCode=19">1R</a>'
        f"<h4>2000年1月1日（土）SYNTHETIC 第1競走 14:10発走</h4><h4>単勝・複勝 オッズ （{stage}）</h4>"
        f'<table class="odd_popular_table_02"><tr>{headers}</tr>{rows}</table>'
    ).encode()


def receipt(raw, minute=0):
    return {
        "url": "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/OddsTanFuku?k_raceDate=2000%2F01%2F01&k_raceNo=1&k_babaCode=19",
        "status": 200,
        "sha256": sha(raw),
        "bytes": len(raw),
        "fetch_started_at": f.at(minute),
        "headers_received_at": f.at(minute, 1),
        "collector_received_at": f.at(minute, 2),
        "raw_saved_at": f.at(minute, 3),
    }


def test_final_and_exclusion_are_labels_not_completion_or_active_proof():
    state = parse_state_page(page(), f.RACE)
    assert state["odds_stage"] == "FINAL_DISPLAYED"
    assert state["runners"]["2"] == {"change_label": "競走除外", "status": "EXCLUDED", "active": False}
    assert state["runners"]["1"]["active"] is None
    assert not state["paper_eligible"] and not state["settlement_final"]
    assert state["source_updated_at"] is None and state["pre_race_evidence"] is None
    unknown = parse_state_page(page(stage="13:55現在", change="不明変更"), f.RACE)
    assert unknown["odds_stage"] == "CLOCK_DISPLAYED" and not unknown["paper_eligible"]
    assert unknown["displayed_time_of_day"] == "13:55"
    assert unknown["reason"] == "DISPLAY_DATE_UNQUALIFIED"
    assert unknown["runners"]["2"]["status"] == "UNKNOWN_CHANGE"
    assert unknown["source_updated_at"] is None


def test_live_video_link_is_not_the_selected_odds_navigation():
    raw = page(stage="13:55現在", change="")
    video = '<a class="cNaviBtn live" href="https://video.example/live">ライブ中継</a>'
    assert parse_state_page(video.encode() + raw, f.RACE) == parse_state_page(raw, f.RACE)


@pytest.mark.parametrize('href', [
    '/KeibaWeb/TodayRaceInfo/OddsTanFuku?k_raceDate=2000%2F01%2F01&k_raceNo=1&k_babaCode=19',
    'https://other.example/KeibaWeb/TodayRaceInfo/OddsTanFuku?k_raceDate=2000%2F01%2F01&k_raceNo=1&k_babaCode=19',
    '/KeibaWeb/TodayRaceInfo/OddsTanFuku?k_raceDate=2000%2F01%2F01&k_raceNo=2&k_babaCode=19',
])
def test_duplicate_or_foreign_selected_odds_tabs_still_quarantine(store, href):
    raw = f'<a class="cNaviBtn live" href="{href}">単・複</a>'.encode() + page()
    assert StateEvidence(store).ingest(receipt(raw), raw, f.RACE)['status'] == 'QUARANTINED'


@pytest.mark.parametrize('stage', ['24:00現在', '13:60現在', '13:55更新', '13:55現在最終'])
def test_unknown_clock_heading_does_not_become_an_update_time(stage):
    state = parse_state_page(page(stage=stage), f.RACE)
    assert state['odds_stage'] == 'UNKNOWN'
    assert state['displayed_time_of_day'] is None and state['source_updated_at'] is None
    assert not state['paper_eligible'] and state['pre_race_evidence'] is None


def test_clock_after_midnight_never_infers_update_date_from_race_or_receipt(store):
    raw = page(stage='23:55現在', change='')
    r = receipt(raw)
    for i, field in enumerate(['fetch_started_at', 'headers_received_at', 'collector_received_at', 'raw_saved_at']):
        r[field] = f'1999-12-31T15:05:0{i}+00:00'  # 2000-01-01 00:05 JST
    store.clock = lambda: '1999-12-31T15:06:00+00:00'
    state = StateEvidence(store).ingest(r, raw, f.RACE)
    assert state['status'] == 'OBSERVED_UNQUALIFIED'
    assert state['displayed_time_of_day'] == '23:55'
    assert state['source_updated_at'] is None
    assert state['reason'] == 'DISPLAY_DATE_UNQUALIFIED' and not state['paper_eligible']
    assert all(runner['active'] is None for runner in state['runners'].values())


def test_navigation_repair_is_available_only_after_reparse(store):
    raw = b'<a class="cNaviBtn live" href="https://video.example/live">video</a>' + page(stage='13:55現在')
    def old_parser(raw, race):
        raise ValueError('STATE_CURRENT_LINK')
    evidence = StateEvidence(store)
    old = evidence.ingest(receipt(raw), raw, f.RACE, version='synthetic-old-navigation', parser=old_parser)
    before = evidence.asof(f.RACE, old['available_at'])
    assert old['status'] == 'QUARANTINED'
    store.clock = lambda: f.at(8)
    repaired = evidence.ingest(receipt(raw), raw, f.RACE)
    assert repaired['status'] == 'OBSERVED_UNQUALIFIED' and repaired['odds_stage'] == 'CLOCK_DISPLAYED'
    assert repaired['available_at'] == stamp(f.at(8))
    assert evidence.asof(f.RACE, old['available_at']) == before
    assert evidence.ingest(receipt(raw), raw, f.RACE) == repaired
    assert store.db.execute('SELECT count(*) FROM race_state_observations').fetchone()[0] == 1
    assert len(evidence.history(f.RACE, f.at(8))['history']) == 2


def test_real_page_frame_span_with_hidden_placeholder_keeps_horse_columns():
    html = page().decode().replace("<tr><td>1</td><td>1</td>", '<tr><td rowspan="2">1</td><td>1</td>')
    html = html.replace("<tr><td>1</td><td>2</td>", '<tr><td style="display:none">1</td><td>2</td>')
    assert parse_state_page(html.encode(), f.RACE) == parse_state_page(page(), f.RACE)
    html = html.replace("3着払い", "2着払い")
    assert parse_state_page(html.encode(), f.RACE)["runners"]["2"]["active"] is False


@pytest.mark.parametrize("code", ["00", "99", "20"])
def test_receipt_venue_must_match_page_current_race_navigation(store, code):
    raw = page()
    r = receipt(raw)
    r["url"] = r["url"].replace("k_babaCode=19", "k_babaCode=" + code)
    report = StateEvidence(store).ingest(r, raw, f.RACE)
    assert report["status"] == "QUARANTINED" and "runners" not in report


@pytest.mark.parametrize(
    "wrapper",
    [
        "<span hidden>{}</span>",
        '<span style="display: none">{}</span>',
        '<span aria-hidden="true">{}</span>',
        '<span style="visibility:hidden!important">{}</span>',
        "<template>{}</template>",
        "<script>{}</script>",
    ],
)
def test_hidden_change_text_is_not_observed_exclusion(store, wrapper):
    raw = page().decode().replace("競走除外", wrapper.format("競走除外")).encode()
    result = StateEvidence(store).ingest(receipt(raw), raw, f.RACE)
    assert result["status"] == "QUARANTINED" and "runners" not in result


@pytest.mark.parametrize(
    "before,after",
    [
        ("<h4>単勝", "<h4 hidden>単勝"),
        ("<table class=", "<div hidden><table class="),
        ('<a class="cNaviBtn live"', '<a hidden class="cNaviBtn live"'),
    ],
)
def test_hidden_heading_table_or_current_link_is_quarantined(store, before, after):
    raw = page().decode().replace(before, after).encode()
    assert StateEvidence(store).ingest(receipt(raw), raw, f.RACE)["status"] == "QUARANTINED"


def test_repeat_identical_body_later_observation_and_reparse_keep_old_asof(store):
    raw = page()
    states = StateEvidence(store)
    r = receipt(raw)
    first = states.ingest(r, raw, f.RACE)
    assert states.asof(f.RACE, f.at(1))["evidence"] is None
    before = states.asof(f.RACE, f.at(3))
    store.clock = lambda: f.at(8)
    assert states.ingest(r, raw, f.RACE) == first
    second = states.ingest(receipt(raw, 4), raw, f.RACE)
    assert first["observation_id"] != second["observation_id"]
    assert first["raw_hash"] == second["raw_hash"]
    assert states.asof(f.RACE, f.at(3)) == before
    assert states.asof(f.RACE, f.at(8))["evidence"] == second
    store.clock = lambda: f.at(10)
    reparsed = states.ingest(r, raw, f.RACE, version="synthetic-reparse-v2")
    assert reparsed["available_at"] == stamp(f.at(10))
    assert states.asof(f.RACE, f.at(3)) == before
    assert states.asof(f.RACE, f.at(10))["evidence"] == second
    assert states.history(f.RACE, f.at(3))["history"] == [first]
    assert len(states.history(f.RACE, f.at(10))["history"]) == 3
    assert store.db.execute("SELECT count(*) FROM race_state_observations").fetchone()[0] == 2
    assert store.db.execute("SELECT count(*) FROM race_state_parses").fetchone()[0] == 3
    assert store.metrics()["observations"] == 0
    assert store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    reopened = Store(store.root)
    assert StateEvidence(reopened).asof(f.RACE, f.at(3)) == before
    reopened.close()


def test_future_final_page_cannot_change_earlier_evidence(store):
    states = StateEvidence(store)
    raw = page(stage="未確認")
    first = states.ingest(receipt(raw), raw, f.RACE)
    store.clock = lambda: f.at(8)
    final = page()
    second = states.ingest(receipt(final, 4), final, f.RACE)
    assert states.asof(f.RACE, f.at(3))["evidence"] == first
    assert states.asof(f.RACE, f.at(8))["evidence"] == second
    assert first["odds_stage"] == "UNKNOWN" and second["odds_stage"] == "FINAL_DISPLAYED"


@pytest.mark.parametrize(
    "before,after",
    [
        ("第1競走", "第2競走"),
        ("SYNTHETIC 第", "OTHER 第"),
        ("1月1日", "1月2日"),
        ("14:10", "25:10"),
        ("馬番", "番号"),
        ('colspan="2"', 'colspan="3"'),
        ("<td>2</td>", "<td>1</td>"),
        ("<td>4</td>", "<td>99</td>"),
        ("<td>4</td>", '<td rowspan="2">4</td>'),
        ("</table>", "<tr><td>孤立</table>"),
        ("</table>", "<td>孤立</td></table>"),
        ("</table>", ""),
        ("odd_popular_table_02", "unknown_table"),
    ],
)
def test_wrong_identity_unknown_structure_and_partial_rows_quarantine_raw(store, before, after):
    raw = page().decode().replace(before, after).encode()
    result = StateEvidence(store).ingest(receipt(raw), raw, f.RACE)
    assert result["status"] == "QUARANTINED"
    assert store.read_body(result["raw_hash"], "raw") == raw
    assert not result["paper_eligible"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", 403),
        ("bytes", 1),
        ("sha256", "0" * 64),
        ("headers_received_at", None),
        ("raw_saved_at", "2001-01-01T00:00:00Z"),
        ("collector_received_at", "1999-01-01T00:00:00Z"),
        ("url", "https://other.example/"),
    ],
)
def test_invalid_receipt_creates_no_state_observation(store, field, value):
    raw = page()
    r = receipt(raw)
    r[field] = value
    states = StateEvidence(store)
    with pytest.raises(ValueError):
        states.ingest(r, raw, f.RACE)
    assert store.db.execute("SELECT count(*) FROM race_state_observations").fetchone()[0] == 0


def test_same_event_changed_body_conflicts_instead_of_rewriting(store):
    states = StateEvidence(store)
    raw = page()
    first = states.ingest(receipt(raw), raw, f.RACE)
    changed = page(change="不明変更")
    with pytest.raises(ValueError, match="STATE_EVENT_CONFLICT"):
        states.ingest(receipt(changed), changed, f.RACE)
    assert states.asof(f.RACE, f.at(3))["evidence"] == first


def test_publication_clock_is_after_report_storage_and_survives_replay(store):
    states = StateEvidence(store)
    raw = page()
    clock = [f.at(2)]
    store.clock = lambda: clock[0]
    body = store.body

    def delayed(data, kind):
        result = body(data, kind)
        if kind == "reports":
            clock[0] = f.at(3)
        return result

    store.body = delayed
    result = states.ingest(receipt(raw), raw, f.RACE)
    assert result["parsed_at"] == stamp(f.at(2)) and result["available_at"] == stamp(f.at(3))
    assert states.asof(f.RACE, f.at(2))["evidence"] is None
    clock[0] = f.at(5)
    assert states.ingest(receipt(raw), raw, f.RACE) == result


def test_cli_does_not_print_state_body_and_refuses_public_ci(tmp_path, monkeypatch, capsys):
    from hr_platform import cli

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "private_root", lambda _: tmp_path / "store")
    raw = page()
    html = tmp_path / "page.html"
    html.write_bytes(raw)
    r = tmp_path / "receipt.json"
    r.write_text(json.dumps(receipt(raw)))
    args = ["import-state", "--html", str(html), "--receipt", str(r), "--race", f.RACE]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert set(summary) == {"status", "private_report"} and "競走除外" not in out
    monkeypatch.setenv("CI", "true")
    assert cli.main(args) == 2
