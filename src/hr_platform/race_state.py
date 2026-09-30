"""Observed labels from the official win/place page, with an availability timeline.

FINAL describes the displayed odds, not race completion or settlement finality.
Unverified headings and empty change fields never establish PRE_RACE or active runners.
"""

from datetime import datetime
import csv
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
from .common import canonical, identity, sha, stamp, seconds

VERSION = "nar-odds-page-state-v5"
MAX_BYTES = 2 * 1024 * 1024
HEADERS = [
    "枠",
    "馬番",
    "馬名",
    "単勝オッズ",
    "複勝オッズ(3着払い)",
    "性齢",
    "馬体重(増減)",
    "負担重量",
    "騎手(所属)",
    "所属",
    "調教師",
    "変更情報",
]
SCHEMA = """
CREATE TABLE IF NOT EXISTS race_state_observations(
 id TEXT PRIMARY KEY, race_id TEXT NOT NULL, raw_hash TEXT NOT NULL,
 receipt_hash TEXT NOT NULL, received_at TEXT NOT NULL, raw_saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS race_state_parses(
 id TEXT PRIMARY KEY, observation_id TEXT NOT NULL, version TEXT NOT NULL,
 parsed_at TEXT NOT NULL, available_at TEXT, report_hash TEXT NOT NULL,
 UNIQUE(observation_id,version));
"""


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


class OddsPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.heading = None
        self.headings = []
        self.inside = False
        self.tables = 0
        self.row = None
        self.cell = None
        self.rows = []
        self.current_links = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = compact(attrs.get("style", "")).lower()
        hidden = (
            any(x[1] for x in self.elements)
            or "hidden" in attrs
            or attrs.get("aria-hidden", "").lower() == "true"
            or bool(
                re.search(
                    r"(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)", style
                )
            )
            or tag in {"script", "style", "template"}
        )
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.elements.append((tag, hidden))
        frame_cell = (
            self.inside and self.row == [] and (tag == "td" or self.cell and self.cell["tag"] == "td")
        )
        # "live" also marks the unrelated video link. Identify the selected
        # odds tab by its path; validate its origin and race below.
        current_link = (
            tag == "a"
            and {"cNaviBtn", "live"}.issubset(attrs.get("class", "").split())
            and urlsplit(attrs.get("href", "")).path == "/KeibaWeb/TodayRaceInfo/OddsTanFuku"
        )
        protected = (
            tag == "h4"
            or self.heading is not None
            or current_link
            or self.inside
            and (tag in {"table", "tr", "th"} or (tag == "td" or self.cell is not None) and not frame_cell)
            or tag == "table"
            and "odd_popular_table_02" in attrs.get("class", "").split()
        )
        if hidden and protected:
            raise ValueError("STATE_HIDDEN_EVIDENCE")
        if current_link:
            self.current_links.append(attrs.get("href", ""))
        if tag == "h4":
            if self.heading is not None:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.heading = ""
        if tag == "table":
            if self.inside:
                raise ValueError("STATE_PAGE_STRUCTURE")
            if "odd_popular_table_02" in attrs.get("class", "").split():
                self.inside = True
                self.tables += 1
        if self.inside and tag == "tr":
            if self.row is not None:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.row = []
        if self.inside and tag in {"th", "td"}:
            if self.cell is not None or self.row is None:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.cell = {
                "tag": tag,
                "span": attrs.get("colspan", "1"),
                "rowspan": attrs.get("rowspan", "1"),
                "text": "",
            }

    def handle_data(self, data):
        if self.heading is not None:
            self.heading += data
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        matching = [i for i, x in enumerate(self.elements) if x[0] == tag]
        if matching:
            del self.elements[matching[-1] :]
        if tag == "h4" and self.heading is not None:
            self.headings.append(compact(self.heading))
            self.heading = None
        if self.inside and tag in {"td", "th"}:
            if self.cell is None or self.cell["tag"] != tag:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.row.append(self.cell)
            self.cell = None
        if self.inside and tag == "tr":
            if self.row is None or self.cell is not None:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.rows.append(self.row)
            self.row = None
        if self.inside and tag == "table":
            if self.row is not None or self.cell is not None:
                raise ValueError("STATE_PAGE_STRUCTURE")
            self.inside = False


def parse_state_page(raw, race_id):
    page = OddsPage()
    page.feed(raw.decode("utf-8-sig", errors="strict"))
    if (
        page.tables != 1
        or page.inside
        or page.row is not None
        or page.cell is not None
        or page.heading is not None
        or len(page.rows) < 2
    ):
        raise ValueError("STATE_PAGE_STRUCTURE")
    identities = []
    for h in page.headings:
        m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日\([^)]*\)(.+)第(\d+)競走(\d{2}:\d{2})発走(\(変更\))?", h)
        if m:
            year, month, day, venue, number, start, changed = m.groups()
            date = f"{year}{int(month):02}{int(day):02}"
            dt = datetime.strptime(date + start, "%Y%m%d%H:%M").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            identities.append((f"{date}:{venue}:{int(number)}", dt.isoformat(), changed is not None))
    if len(identities) != 1 or identities[0][0] != race_id:
        raise ValueError("STATE_RACE_IDENTITY")
    if len(page.current_links) != 1:
        raise ValueError("STATE_CURRENT_LINK")
    current = urlsplit(page.current_links[0])
    query = parse_qs(current.query, strict_parsing=True)
    date, _, number = race_id.split(":")
    if (
        current.scheme
        or current.netloc
        or current.fragment
        or current.path != "/KeibaWeb/TodayRaceInfo/OddsTanFuku"
        or set(query) != {"k_raceDate", "k_raceNo", "k_babaCode"}
        or query["k_raceDate"] != [f"{date[:4]}/{date[4:6]}/{date[6:]}"]
        or query["k_raceNo"] != [number]
        or len(query["k_babaCode"]) != 1
        or not re.fullmatch(r"[0-9]{2}", query["k_babaCode"][0])
    ):
        raise ValueError("STATE_CURRENT_LINK")
    headings = [h for h in page.headings if h.startswith("単勝・複勝オッズ")]
    if len(headings) != 1:
        raise ValueError("STATE_ODDS_HEADING")
    final = headings[0] == "単勝・複勝オッズ(最終)"
    clock = re.fullmatch(r"単勝・複勝オッズ\(((?:[01][0-9]|2[0-3]):[0-5][0-9])現在\)", headings[0])
    header = page.rows[0]
    labels = [compact(c["text"]) for c in header]
    if (
        len(labels) != len(HEADERS)
        or labels[:4] != HEADERS[:4]
        or labels[5:] != HEADERS[5:]
        or labels[4] not in {"複勝オッズ(2着払い)", "複勝オッズ(3着払い)"}
        or any(c["tag"] != "th" or c["rowspan"] != "1" for c in header)
        or [c["span"] for c in header] != ["1"] * 4 + ["2"] + ["1"] * 7
    ):
        raise ValueError("STATE_TABLE_HEADER")
    runners = {}
    for row in page.rows[1:]:
        # The real page keeps a hidden frame cell under a frame rowspan. Horse
        # number and change columns remain fixed; frame values are not used here.
        if (
            len(row) != 13
            or any(c["tag"] != "td" or c["span"] != "1" for c in row)
            or row[0]["rowspan"] not in {"1", "2"}
            or any(c["rowspan"] != "1" for c in row[1:])
        ):
            raise ValueError("STATE_RUNNER_ROW")
        number, change = compact(row[1]["text"]), compact(row[12]["text"])
        if not re.fullmatch(r"[1-9][0-9]?", number) or not 1 <= int(number) <= 16 or number in runners:
            raise ValueError("STATE_RUNNER_ID")
        excluded = change == "競走除外"
        runners[number] = {
            "change_label": change,
            "status": "EXCLUDED" if excluded else "NO_CHANGE_DISPLAYED" if not change else "UNKNOWN_CHANGE",
            "active": False if excluded else None,
        }
    return {
        "race_id": race_id,
        "venue_code": query["k_babaCode"][0],
        "scheduled_start_at": identities[0][1],
        "schedule_change_displayed": identities[0][2],
        "odds_heading": headings[0],
        "odds_stage": "FINAL_DISPLAYED" if final else "CLOCK_DISPLAYED" if clock else "UNKNOWN",
        "reason": "FINAL_ODDS_DISPLAYED" if final else "DISPLAY_DATE_UNQUALIFIED" if clock else "ODDS_STAGE_UNQUALIFIED",
        # The heading contains a time of day, not an unambiguous update date.
        # Neither the race date nor the receipt date establishes that date.
        "displayed_time_of_day": clock[1] if clock else None,
        "runners": runners,
        "source_updated_at": None,
        "pre_race_evidence": None,
        "paper_eligible": False,
        "settlement_final": False,
    }


def validate_body_times(receipt, raw, now, limit=MAX_BYTES):
    if (
        receipt.get("status") != 200
        or receipt.get("sha256") != sha(raw)
        or type(receipt.get("bytes")) is not int
        or receipt["bytes"] != len(raw)
        or len(raw) > limit
    ):
        raise ValueError("STATE_RECEIPT_BODY")
    fields = ("fetch_started_at", "headers_received_at", "collector_received_at", "raw_saved_at")
    if any(not isinstance(receipt.get(k), str) for k in fields):
        raise ValueError("STATE_RECEIPT_TIME")
    times = [stamp(receipt[k]) for k in fields]
    if times != sorted(times) or times[-1] > now:
        raise ValueError("STATE_RECEIPT_TIME")
    return times[0]


def validate_receipt(receipt, raw, race_id, now, path="/KeibaWeb/TodayRaceInfo/OddsTanFuku"):
    started = validate_body_times(receipt, raw, now)
    validate_page_url(receipt["url"], race_id, path)
    return identity([receipt["url"], started])


def validate_page_url(value, race_id, path):
    url = urlsplit(value)
    query = parse_qs(url.query, strict_parsing=True)
    date, _, number = race_id.split(":")
    datetime.strptime(date, "%Y%m%d")
    if (
        url.scheme != "https"
        or url.netloc != "www.keiba.go.jp"
        or url.fragment
        or url.path != path
        or set(query) != {"k_raceDate", "k_raceNo", "k_babaCode"}
        or query["k_raceDate"] != [f"{date[:4]}/{date[4:6]}/{date[6:]}"]
        or query["k_raceNo"] != [number]
        or len(query["k_babaCode"]) != 1
        or not re.fullmatch(r"[0-9]{2}", query["k_babaCode"][0])
    ):
        raise ValueError("STATE_RECEIPT_URL")


class StateEvidence:
    # Fixed subclasses may share the observation/publication timeline, but use
    # separate tables and parsers so result evidence never enters odds state.
    table_prefix = "race_state"
    scope_key = "race_id"
    receipt_path = "/KeibaWeb/TodayRaceInfo/OddsTanFuku"
    version = VERSION
    parser = staticmethod(parse_state_page)

    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA.replace("race_state", self.table_prefix).replace("race_id", self.scope_key))

    def validate(self, receipt, raw, scope, now):
        return validate_receipt(receipt, raw, scope, now, self.receipt_path)

    def verify_parsed(self, parsed, receipt):
        if parsed["venue_code"] != parse_qs(urlsplit(receipt["url"]).query)["k_babaCode"][0]:
            raise ValueError("STATE_VENUE_CODE")

    def published(self, parse_id):
        row = self.store.db.execute(f"SELECT * FROM {self.table_prefix}_parses WHERE id=?", (parse_id,)).fetchone()
        if row["available_at"] is None:
            available = stamp(self.store.clock())
            if available < row["parsed_at"]:
                raise ValueError("STATE_CLOCK_ORDER")
            with self.store.db:
                self.store.db.execute(
                    f"UPDATE {self.table_prefix}_parses SET available_at=? WHERE id=? AND available_at IS NULL",
                    (available, parse_id),
                )
            row = self.store.db.execute(f"SELECT * FROM {self.table_prefix}_parses WHERE id=?", (parse_id,)).fetchone()
        return {
            **json.loads(self.store.read_body(row["report_hash"], "reports")),
            "available_at": row["available_at"],
        }

    def ingest(self, receipt, raw, race_id, version=None, parser=None):
        version = version or self.version
        parser = parser or self.parser
        now = stamp(self.store.clock())
        event = self.validate(receipt, raw, race_id, now)
        digest, receipt_hash = sha(raw), identity(receipt)
        old = self.store.db.execute(f"SELECT * FROM {self.table_prefix}_observations WHERE id=?", (event,)).fetchone()
        if old and (old["receipt_hash"] != receipt_hash or old[self.scope_key] != race_id):
            raise ValueError("STATE_EVENT_CONFLICT")
        parse_id = identity([event, version])
        existing = self.store.db.execute(
            f"SELECT id FROM {self.table_prefix}_parses WHERE id=?", (parse_id,)
        ).fetchone()
        if existing:
            return self.published(parse_id)
        self.store.body(raw, "raw")
        self.store.body(canonical(receipt), "receipts")
        saved_at = stamp(self.store.clock())
        if saved_at < now:
            raise ValueError("STATE_CLOCK_ORDER")
        with self.store.db:
            self.store.db.execute(
                f"INSERT OR IGNORE INTO {self.table_prefix}_observations VALUES(?,?,?,?,?,?)",
                (event, race_id, digest, receipt_hash, stamp(receipt["collector_received_at"]), saved_at),
            )
        registered = self.store.db.execute(
            f"SELECT * FROM {self.table_prefix}_observations WHERE id=?", (event,)
        ).fetchone()
        if registered["receipt_hash"] != receipt_hash or registered[self.scope_key] != race_id:
            raise ValueError("STATE_EVENT_CONFLICT")
        report = {
            "id": parse_id,
            "observation_id": event,
            "version": version,
            "raw_hash": digest,
            "receipt_hash": receipt_hash,
            self.scope_key: race_id,
            "received_at": stamp(receipt["collector_received_at"]),
            "paper_eligible": False,
            "status": "OBSERVED_UNQUALIFIED",
        }
        try:
            parsed = parser(raw, race_id)
            self.verify_parsed(parsed, receipt)
            report.update(parsed)
        except (ValueError, UnicodeError, csv.Error):
            report.update(status="QUARANTINED", reason="STATE_PAGE_UNQUALIFIED")
        parsed_at = stamp(self.store.clock())
        if parsed_at < saved_at:
            raise ValueError("STATE_CLOCK_ORDER")
        report.update(parsed_at=parsed_at)
        report_hash = self.store.body(canonical(report), "reports")
        with self.store.db:
            self.store.db.execute(
                f"INSERT OR IGNORE INTO {self.table_prefix}_parses VALUES(?,?,?,?,?,?)",
                (parse_id, event, version, parsed_at, None, report_hash),
            )
        return self.published(parse_id)

    def asof(self, race_id, at):
        at = stamp(at)
        row = self.store.db.execute(
            f"""SELECT p.report_hash,p.available_at FROM {self.table_prefix}_parses p
            JOIN {self.table_prefix}_observations o ON o.id=p.observation_id
            WHERE o.{self.scope_key}=? AND p.available_at<=?
            ORDER BY o.received_at DESC,p.available_at DESC,p.id DESC LIMIT 1""",
            (race_id, at),
        ).fetchone()
        evidence = (
            {
                **json.loads(self.store.read_body(row["report_hash"], "reports")),
                "available_at": row["available_at"],
            }
            if row
            else None
        )
        return {
            self.scope_key: race_id,
            "asof_at": at,
            "evidence": evidence,
            "age_seconds": seconds(at, evidence["received_at"]) if evidence else None,
        }

    def history(self, race_id, at):
        at = stamp(at)
        rows = self.store.db.execute(
            f"""SELECT p.report_hash,p.available_at FROM {self.table_prefix}_parses p
            JOIN {self.table_prefix}_observations o ON o.id=p.observation_id
            WHERE o.{self.scope_key}=? AND p.available_at<=?
            ORDER BY o.received_at,p.available_at,p.id""",
            (race_id, at),
        ).fetchall()
        return {
            self.scope_key: race_id,
            "asof_at": at,
            "history": [
                {
                    **json.loads(self.store.read_body(row["report_hash"], "reports")),
                    "available_at": row["available_at"],
                }
                for row in rows
            ],
        }
