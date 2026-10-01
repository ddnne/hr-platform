"""Official manual-shaped race files; live semantics remain to be qualified.

Result columns are isolated from candidate pre-race metadata. Empty results do not
prove PRE_RACE, and displayed payouts do not prove finality or refund coverage.
"""

import csv
from datetime import datetime, timedelta, timezone
import io
import unicodedata
from .parser import unzip, UNORDERED

KEYS = ["競馬場", "競走年月日", "レース番号"]
RACE_HEADERS = [
    *KEYS,
    "発走時刻",
    "競走種類名称",
    "レース名",
    *[f"副賞名{i}" for i in range(1, 16)],
    "芝ダート区分",
    "回り",
    "距離",
    "天候",
    "馬場",
    "頭数",
    "条件",
    *[f"{i}着賞金(円)" for i in range(1, 6)],
    "上がり4F",
    "上がり3F",
    *[f"ハロンタイム{i}" for i in range(1, 16)],
    *[f"コーナー名称{i}" for i in range(1, 9)],
    *[f"コーナー通過順{i}" for i in range(1, 9)],
]
HORSE_HEADERS = [
    *KEYS,
    "枠番",
    "帽色",
    "馬番",
    "馬名",
    "性",
    "齢",
    "毛色",
    "生年月日",
    "父馬名",
    "母馬名",
    "母父馬名",
    "騎手名",
    "騎手所属",
    "負担重量",
    "騎手成績",
    "調教師",
    "調教師所属",
    "馬主氏名",
    "生産牧場名",
    "馬体重",
    "馬体重増減",
    "全成績",
    "ダート左成績",
    "ダート右成績",
    "当競馬場成績",
    "うち当距離成績",
    "最高タイム",
    "最高タイム良馬場",
    "着順",
    "タイム",
    "着差",
    "上がり3F",
    "人気",
]
# (market, selection columns, amount, popularity) in official CSV order.
PAYOUT_GROUPS = [
    ("win", ["単勝組番"], "単勝払戻金（円）", "単勝人気"),
    *[("place", [f"複勝組番{i}"], f"複勝払戻金{i}（円）", f"複勝人気{i}") for i in range(1, 4)],
    *[
        (market, [f"{label}組番1", f"{label}組番2"], f"{label}払戻金（円）", popularity)
        for market, label, popularity in [
            ("bracket_quinella", "枠複", "枠複人気"),
            ("bracket_exacta", "枠単", "枠単人気"),
            ("quinella", "馬複", "馬複人気１"),
            ("exacta", "馬単", "馬単人気１"),
        ]
    ],
    *[
        ("wide", [f"ワイド組番{i}馬番1", f"ワイド組番{i}馬番2"], f"ワイド払戻金{i}（円）", f"ワイド人気{i}")
        for i in range(1, 4)
    ],
    *[
        (market, [f"{label}組番馬番{i}" for i in range(1, 4)], f"{label}払戻金（円）", f"{label}人気")
        for market, label in [("trio", "３連複"), ("trifecta", "３連単")]
    ],
]
PAYOUT_HEADERS = [
    *KEYS,
    "レース名",
    *[c for _, selection, amount, popularity in PAYOUT_GROUPS for c in [*selection, amount, popularity]],
]
VERSION = "nar-race-manual-20260916-v2"


def normalize(value):
    return unicodedata.normalize("NFKC", value)


def rows(data, headers, encoding):
    if encoding not in {"utf-8-sig", "cp932"}:
        raise ValueError("ENCODING_UNQUALIFIED")
    reader = csv.reader(io.StringIO(data.decode(encoding, errors="strict")))
    actual = next(reader, None)
    if actual is None or [normalize(x) for x in actual] != [normalize(x) for x in headers]:
        raise ValueError("SCHEMA_CHANGED")
    result = []
    for values in reader:
        if len(values) != len(headers):
            raise ValueError("SCHEMA_ROW_WIDTH")
        result.append(dict(zip(headers, values)))
    return result


def race_id(row):
    # Do not silently combine different dates or invalid race numbers.
    date = row["競走年月日"]
    if len(date) != 8 or not date.isascii() or not date.isdigit():
        raise ValueError("RACE_DATE")
    datetime.strptime(date, "%Y%m%d")
    no = int(row["レース番号"])
    if not row["競馬場"] or not 1 <= no <= 12:
        raise ValueError("RACE_KEY")
    return f"{date}:{row['競馬場']}:{no}"


def parse_race_bundle(raw, encoding="utf-8-sig"):
    files = unzip(raw)
    tables = {}
    for suffix, headers in [
        ("racelist", RACE_HEADERS),
        ("horselist", HORSE_HEADERS),
        ("payback", PAYOUT_HEADERS),
    ]:
        found = [data for name, data in files.items() if name.endswith(f"_{suffix}.csv")]
        if len(found) != 1:
            raise ValueError("RACE_FILE_COUNT")
        tables[suffix] = rows(found[0], headers, encoding)
    if len(files) != 3:
        raise ValueError("UNEXPECTED_ZIP_MEMBER")
    races = {}
    for row in tables["racelist"]:
        key = race_id(row)
        if key in races:
            raise ValueError("DUPLICATE_RACE")
        start = row["発走時刻"]
        scheduled = None
        if start:
            if len(start) != 4 or not start.isascii() or not start.isdigit():
                raise ValueError("START_TIME")
            scheduled = (
                datetime.strptime(row["競走年月日"] + start, "%Y%m%d%H%M")
                .replace(tzinfo=timezone(timedelta(hours=9)))
                .isoformat()
            )
        count = int(row["頭数"]) if row["頭数"] else None
        if count is not None and not 0 <= count <= 16:
            raise ValueError("RUNNER_COUNT")
        results = {name: row[name] for name in RACE_HEADERS[33:] if row[name]}
        races[key] = {
            "scheduled_start_at": scheduled,
            "declared_runner_count": count,
            "surface_label": row["芝ダート区分"],
            "condition_label": row["条件"],
            "race_results": results,
            "horses": {},
            "payout_tickets": [],
            "status": "UNKNOWN",
            "pre_race_evidence": None,
            "final": False,
            "complete_markets": [],
            "refund_coverage": "UNKNOWN",
        }
    for row in tables["horselist"]:
        key = race_id(row)
        if key not in races:
            raise ValueError("ORPHAN_HORSE")
        horse = int(row["馬番"])
        if not 1 <= horse <= 16 or str(horse) in races[key]["horses"]:
            raise ValueError("HORSE_KEY")
        races[key]["horses"][str(horse)] = {
            "frame": row["枠番"],
            "result_fields": {name: row[name] for name in HORSE_HEADERS[31:] if row[name]},
            # A row is an entry, not proof the horse remains active.
            "active": None,
        }
    tickets = {}
    for row in tables["payback"]:
        key = race_id(row)
        if key not in races:
            raise ValueError("ORPHAN_PAYOUT")
        for market, columns, amount, _ in PAYOUT_GROUPS:
            values = [row[c].strip() for c in columns]
            value = row[amount].strip()
            if not any([*values, value]):
                continue
            # Return/special-payment markers are preserved in the raw body, never interpreted as zero.
            if not all(v.isascii() and v.isdigit() for v in [*values, value]):
                raise ValueError("PAYOUT_ENCODING_UNQUALIFIED")
            selection = tuple(int(v) for v in values)
            if any(not 1 <= n <= (8 if market.startswith("bracket") else 16) for n in selection):
                raise ValueError("PAYOUT_SELECTION")
            if not market.startswith("bracket") and len(set(selection)) != len(selection):
                raise ValueError("PAYOUT_SELECTION")
            if market in UNORDERED:
                selection = tuple(sorted(selection))
            selection = "-".join(map(str, selection))
            paid = int(value)
            if paid < 1:
                raise ValueError("PAYOUT_AMOUNT")
            ticket_key = (key, market, selection)
            if ticket_key in tickets and tickets[ticket_key] != paid:
                raise ValueError("CONFLICTING_PAYOUT")
            tickets[ticket_key] = paid
    for (key, market, selection), paid in tickets.items():
        races[key]["payout_tickets"].append(
            {"market": market, "selection": selection, "payout_per_100": paid}
        )
    for race in races.values():
        race["entry_count_matches"] = race["declared_runner_count"] == len(race["horses"])
        race["result_present"] = bool(
            race["race_results"]
            or race["payout_tickets"]
            or any(any(value for name, value in h["result_fields"].items() if name != "人気")
                   for h in race["horses"].values())
        )
    if not races:
        raise ValueError("EMPTY_RACES")
    return {"version": VERSION, "live_qualified": False, "races": races}
