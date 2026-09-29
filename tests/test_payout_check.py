import json
import pytest
from hr_platform import fixtures as f
from hr_platform.payout_check import parse_result_page, crosscheck
from test_realdata import race_archive


def page(rows=None):
    rows = rows or '<tr><td class="title">馬連複</td><td class="a">2-1</td><td class="refundMoney">650円</td><td class="c">1人気</td></tr>'
    return ('<h4>2000年1月1日（土） SYNTHETIC 第1競走<span>競走成績</span></h4>'
            '<section class="newRefundTable"><table>' + rows + '</table></section>').encode()


def archive():
    return race_archive(payout_rows=[{"馬複組番1": "1", "馬複組番2": "2", "馬複払戻金（円）": "650"}])


def test_crosscheck_preserves_raw_without_observations_or_settlement(store):
    raw, html = archive(), page()
    result = crosscheck(store, raw, "race.zip", html, f.RACE, "utf-8-sig")
    assert result["status"] == "MATCHED_UNQUALIFIED"
    assert not result["settlement_eligible"] and not result["paper_eligible"]
    assert store.read_body(result["html_raw_hash"], "raw") == html
    assert store.read_body(result["csv_raw_hash"], "raw") == raw
    assert store.metrics()["observations"] == 0
    assert store.db.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_mismatch_keeps_both_versions(store):
    result = crosscheck(store, archive(), "race.zip", page().replace(b"650", b"660"), f.RACE, "utf-8-sig")
    assert result["status"] == "MISMATCH"
    assert result["html_tickets"][0]["payout_per_100"] == 660
    assert result["csv_tickets"][0]["payout_per_100"] == 650


def test_rowspan_and_japanese_market_name():
    html = page('<tr><td class="title" rowspan="2">三連複</td><td class="d">3-1-2</td><td class="refundMoney">1,200円</td></tr>'
                '<tr><td class="d">1-2-4</td><td class="refundMoney">800円</td></tr>')
    assert parse_result_page(html, f.RACE) == {("trio", "1-2-3"): 1200, ("trio", "1-2-4"): 800}


@pytest.mark.parametrize("before,after", [
    ("第1競走", "第2競走"), ("SYNTHETIC", "OTHER"), ("1月1日", "1月2日"),
    ("650円", "特払い"), ("650円", "6,50円"), ("2-1", "返還"),
    ("2-1", "1-1"), ("2-1", "1-99"), ("newRefundTable", "unknown"),
    ('class="title"', 'class="title" rowspan="2"'),
])
def test_unsupported_or_wrong_race_is_quarantined(store, before, after):
    html = page().decode().replace(before, after).encode()
    result = crosscheck(store, archive(), "race.zip", html, f.RACE, "utf-8-sig")
    assert result["status"] == "QUARANTINED" and not result["settlement_eligible"]


def test_duplicate_payout_or_missing_title_is_rejected():
    row = '<tr><td class="title">馬連複</td><td class="a">1-2</td><td class="refundMoney">650円</td></tr>'
    with pytest.raises(ValueError):
        parse_result_page(page(row + row), f.RACE)
    with pytest.raises(ValueError):
        parse_result_page(page(row.replace('<td class="title">馬連複</td>', '')), f.RACE)


@pytest.mark.parametrize("broken", [
    '<tr><td class="title">単勝</td><td class="a">1</td><td class="refundMoney">200円</td>',
    '<tr><td class="title">単勝',
    '<td class="title">単勝</td>',
])
def test_unfinished_or_orphan_extra_row_cannot_disappear(store, broken):
    html = page().decode().replace('</table>', broken + '</table>').encode()
    report = crosscheck(store, archive(), "race.zip", html, f.RACE, "utf-8-sig")
    assert report["status"] == "QUARANTINED"


def test_cli_mismatch_has_nonzero_exit(tmp_path, monkeypatch, capsys):
    from hr_platform import cli

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "private_root", lambda _: tmp_path / "store")
    raw, html = tmp_path / "race.zip", tmp_path / "result.html"
    raw.write_bytes(archive())
    html.write_bytes(page().replace(b"650", b"660"))
    assert cli.main(['check-payout', '--race-zip', str(raw), '--html', str(html),
                     '--race', f.RACE, '--encoding', 'utf-8-sig']) == 1
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary['status'] == 'MISMATCH'
    assert set(summary) == {'status', 'private_report'}
