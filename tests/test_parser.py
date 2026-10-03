import io
import zipfile
import pytest
from hr_platform import fixtures as f
from hr_platform.parser import parse_odds, unzip


def rewrite(raw, transform):
    data = next(iter(unzip(raw).values()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("20000101_odds.csv", transform(data))
    return out.getvalue()


def test_unknown_state_cannot_become_prerace():
    race = parse_odds(f.archive(), {})[f.RACE]
    assert race["state"]["status"] == "UNKNOWN"
    assert not race["state"]["runners"]


@pytest.mark.parametrize("raw", [b"<html>captcha</html>", b"PKbroken", b""])
def test_bad_archive(raw):
    with pytest.raises(ValueError):
        unzip(raw)


@pytest.mark.parametrize("name", ["../evil", "/absolute", "a\\evil", "x:y"])
def test_zip_paths(name):
    with pytest.raises(ValueError, match="ZIP_UNSAFE"):
        unzip(f.archive(extra=(name, "bad")))


def test_schema_encoding_duplicates_and_limits():
    with pytest.raises(ValueError, match="SCHEMA_CHANGED"):
        parse_odds(rewrite(f.archive(), lambda b: b.replace("人気".encode(), b"changed")), {})
    with pytest.raises(UnicodeError):
        parse_odds(rewrite(f.archive(), lambda b: b"\xff" + b), {})
    with pytest.raises(ValueError, match="DUPLICATE"):
        parse_odds(rewrite(f.archive(), lambda b: b + b.splitlines(keepends=True)[1]), {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb", b"0" * 100000)
    with pytest.raises(ValueError, match="ZIP_UNSAFE"):
        unzip(out.getvalue())


def test_correct_header_cp932_and_unordered():
    raw = rewrite(f.archive(), lambda b: b.decode("utf-8-sig").encode("cp932"))
    assert "1-2" in parse_odds(raw, {}, "cp932")[f.RACE]["markets"]["quinella"]["quotes"]


def test_grouped_stream_matches_full_parser_but_rejects_split_race_blocks():
    from hr_platform.parser import iter_odds_races

    rows = next(iter(unzip(f.archive()).values())).splitlines(keepends=True)
    second = [line.replace(b'SYNTHETIC', b'SYNTHETIC_TWO') for line in rows[1:]]
    raw = rewrite(f.archive(), lambda _: b''.join(rows + second))
    assert dict(iter_odds_races(raw, {})) == parse_odds(raw, {})
    split = rewrite(f.archive(), lambda _: b''.join(rows[:2] + second + rows[2:]))
    assert len(parse_odds(split, {})) == 2
    with pytest.raises(ValueError, match='NONCONTIGUOUS_RACE'):
        dict(iter_odds_races(split, {}))


def monthly_archive(parts=None):
    body = next(iter(unzip(f.archive()).values()))
    if parts is None:
        parts = {'200001_01_odds.csv': body,
                 '200001_02_odds.csv': body.replace(b'20000101', b'20000102'),
                 '200001_03_odds.csv': body.splitlines(keepends=True)[0]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return out.getvalue()


def test_monthly_parts_share_daily_rows_and_keep_empty_parts():
    from hr_platform.parser import iter_monthly_odds_races
    final = dict(iter_monthly_odds_races(monthly_archive(), {}, '200001'))
    assert final[f.RACE] == parse_odds(f.archive(), {})[f.RACE]
    assert set(final) == {f.RACE, f.RACE.replace('20000101', '20000102')}
    assert all(r['state']['status'] == 'UNKNOWN' for r in final.values())
    with pytest.raises(ValueError, match='ODDS_FILE_COUNT'):
        parse_odds(monthly_archive(), {})


@pytest.mark.parametrize('fault', ['duplicate', 'wrong_month', 'bad_date', 'bad_header', 'foreign_member', 'empty'])
def test_monthly_rejects_ambiguous_or_changed_archives(fault):
    from hr_platform.parser import iter_monthly_odds_races
    body = next(iter(unzip(f.archive()).values()))
    parts = {'200001_01_odds.csv': body}
    if fault == 'duplicate':
        parts['200001_02_odds.csv'] = body
    elif fault in {'wrong_month', 'bad_date'}:
        parts['200001_01_odds.csv'] = body.replace(b'20000101', b'20000201' if fault == 'wrong_month' else b'20000132')
    elif fault == 'bad_header':
        parts['200001_02_odds.csv'] = b'SYNTHETIC changed header\n'
    elif fault == 'foreign_member':
        parts['200001_01_race.csv'] = b'SYNTHETIC foreign body'
    else:
        parts['200001_01_odds.csv'] = body.splitlines(keepends=True)[0]
    with pytest.raises(ValueError):
        dict(iter_monthly_odds_races(monthly_archive(parts), {}, '200001'))
