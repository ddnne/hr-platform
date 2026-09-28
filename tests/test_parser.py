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
