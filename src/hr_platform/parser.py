"""Strict odds CSV adapter. Official row semantics require live-file qualification.

Race state is supplied separately; odds alone never prove pre-race eligibility.
No ZIP entry is written to disk. CSV maximum odds/status are retained, not imputed.
"""

import csv
import io
import math
from pathlib import PurePosixPath
import unicodedata
import zipfile

VERSION = "nar-odds-columns-20260916-v1"
HEADERS = [
    "競馬場",
    "競走年月日",
    "レース番号",
    "賭式",
    "番号1",
    "番号2",
    "番号3",
    "オッズ",
    "オッズ（最大）",
    "人気",
]
MARKETS = {
    "単勝": "win",
    "複勝": "place",
    "枠連複": "bracket_quinella",
    "枠複": "bracket_quinella",
    "枠連単": "bracket_exacta",
    "枠単": "bracket_exacta",
    "馬連複": "quinella",
    "馬複": "quinella",
    "馬連": "quinella",
    "馬連単": "exacta",
    "馬単": "exacta",
    "ワイド": "wide",
    "3連複": "trio",
    "3連単": "trifecta",
}
UNORDERED = {"quinella", "trio", "wide", "bracket_quinella"}
ARITY = {
    "win": 1,
    "place": 1,
    "quinella": 2,
    "exacta": 2,
    "wide": 2,
    "bracket_quinella": 2,
    "bracket_exacta": 2,
    "trio": 3,
    "trifecta": 3,
}
MAX_COMPRESSED = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def unzip(raw):
    if len(raw) > MAX_COMPRESSED or not raw.startswith(b"PK"):
        raise ValueError("NOT_ZIP_OR_TOO_LARGE")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 16 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise ValueError("ZIP_LIMIT")
            names = set()
            result = {}
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or entry.filename in names
                    or entry.is_dir()
                    or entry.flag_bits & 1
                    or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or entry.file_size > max(entry.compress_size, 1) * 300
                ):
                    raise ValueError("ZIP_UNSAFE")
                names.add(entry.filename)
                with archive.open(entry) as stream:
                    data = stream.read(min(entry.file_size + 1, MAX_EXPANDED + 1))
                if len(data) != entry.file_size:
                    raise ValueError("ZIP_SIZE")
                result[entry.filename] = data
            return result
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("ZIP_CORRUPT") from exc


def number(value):
    if not value.strip():
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 1 else None


def parse_odds(raw, race_states, encoding="utf-8-sig"):
    if encoding not in {"utf-8-sig", "cp932"}:
        raise ValueError("ENCODING_UNQUALIFIED")
    files = unzip(raw)
    odds_files = [v for k, v in files.items() if k.endswith("_odds.csv")]
    if len(odds_files) != 1 or len(files) != 1:
        raise ValueError("ODDS_FILE_COUNT")
    reader = csv.reader(io.StringIO(odds_files[0].decode(encoding, errors="strict")))
    if next(reader, None) != HEADERS:
        raise ValueError("SCHEMA_CHANGED")
    races = {}
    seen = set()
    for row in reader:
        if len(row) != len(HEADERS):
            raise ValueError("SCHEMA_ROW_WIDTH")
        venue, date, race_no, label, *rest = row
        race_id = f"{date}:{venue}:{int(race_no)}"
        market = MARKETS.get(unicodedata.normalize("NFKC", label))
        if market is None:
            raise ValueError("UNKNOWN_MARKET")
        selection = tuple(int(v) for v in rest[:3] if v.strip() and int(v) != 0)
        if len(selection) != ARITY[market] or any(x < 1 for x in selection):
            raise ValueError("SELECTION_INVALID")
        if not market.startswith("bracket") and len(set(selection)) != len(selection):
            raise ValueError("REPEATED_HORSE")
        if market in UNORDERED:
            selection = tuple(sorted(selection))
        key = "-".join(str(x) for x in selection)
        if (race_id, market, key) in seen:
            raise ValueError("DUPLICATE_SELECTION_NOT_HISTORY")
        seen.add((race_id, market, key))
        state = race_states.get(race_id)
        if state is None:
            # Retain odds but never infer active runners or pre-race status from odds.
            state = {
                "race_id": race_id,
                "venue": venue,
                "status": "UNKNOWN",
                "runners": [],
                "discipline": "UNKNOWN",
            }
        race = races.setdefault(race_id, {"state": state, "markets": {}})
        quotes = race["markets"].setdefault(market, {"source_updated_at": None, "quotes": {}})
        price, maximum = number(rest[3]), number(rest[4])
        quotes["quotes"][key] = {
            "odds": price,
            "odds_max": maximum,
            "display_status": "FIXED"
            if price is not None and not rest[4].strip()
            else "RANGE"
            if price is not None and maximum is not None
            else "UNKNOWN",
            "raw_odds": rest[3],
            "raw_max": rest[4],
            "popularity": rest[5] or None,
        }
    if not races:
        raise ValueError("EMPTY_ODDS")
    return races
