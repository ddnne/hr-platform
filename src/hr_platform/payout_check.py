"""Compare official result-page and CSV payouts offline, without creating settlements."""

from html.parser import HTMLParser
import re
import unicodedata
from .common import stamp
from .parser import MARKETS, UNORDERED, ARITY

VERSION = "ordinary-payout-crosscheck-v1"


class ResultPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.sections = 0
        self.cell = None
        self.in_row = False
        self.in_table = False
        self.row = []
        self.rows = []
        self.heading = None
        self.headings = []
        self.remaining = 0
        self.market = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h4":
            self.heading = ""
        if tag == "section":
            if self.depth:
                self.depth += 1
            elif "newRefundTable" in attrs.get("class", "").split():
                self.depth = 1
                self.sections += 1
        if self.depth and tag == "table":
            if self.remaining or self.in_table:
                raise ValueError("PAYOUT_ROWSPAN")
            self.in_table = True
            self.market = None
        if self.depth and tag == "tr":
            if self.in_row or not self.in_table:
                raise ValueError("PAYOUT_PAGE_STRUCTURE")
            self.in_row = True
            self.row = []
        if self.depth and tag == "td":
            if not self.in_row or self.cell is not None:
                raise ValueError("PAYOUT_PAGE_STRUCTURE")
            self.cell = {"class": attrs.get("class", ""), "text": "", "span": attrs.get("rowspan", "1")}

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data
        if self.heading is not None:
            self.heading += data

    def handle_endtag(self, tag):
        if tag == "h4" and self.heading is not None:
            self.headings.append(self.heading)
            self.heading = None
        if tag == "td" and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        if tag == "tr" and self.depth:
            if not self.in_row or self.cell is not None:
                raise ValueError("PAYOUT_PAGE_STRUCTURE")
            self.in_row = False
            title = [c for c in self.row if c["class"] == "title"]
            if title:
                if len(title) != 1 or self.remaining:
                    raise ValueError("PAYOUT_ROWSPAN")
                label = unicodedata.normalize("NFKC", title[0]["text"].strip()).replace("三連", "3連")
                if label not in MARKETS or not re.fullmatch(r"[1-9][0-9]?", title[0]["span"]):
                    raise ValueError("PAYOUT_MARKET")
                self.market = MARKETS[label]
                self.remaining = int(title[0]["span"])
            if not self.market or self.remaining < 1:
                raise ValueError("PAYOUT_ROWSPAN")
            self.rows.append((self.market, self.row))
            self.remaining -= 1
        if tag == "table" and self.depth:
            if not self.in_table or self.in_row or self.remaining:
                raise ValueError("PAYOUT_PAGE_STRUCTURE")
            self.in_table = False
        if tag == "section" and self.depth:
            if self.depth == 1 and self.in_table:
                raise ValueError("PAYOUT_PAGE_STRUCTURE")
            self.depth -= 1


def parse_result_page(raw, race_id):
    page = ResultPage()
    page.feed(raw.decode("utf-8-sig", errors="strict"))
    if (page.sections != 1 or page.depth or page.cell is not None or page.remaining
            or page.in_row or page.in_table):
        raise ValueError("PAYOUT_PAGE_STRUCTURE")
    identities = []
    for heading in page.headings:
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", heading))
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日\([^)]*\)(.+)第(\d+)競走競走成績", compact)
        if match:
            year, month, day, venue, number = match.groups()
            identities.append(f"{year}{int(month):02}{int(day):02}:{venue}:{int(number)}")
    if identities != [race_id]:
        raise ValueError("PAYOUT_RACE_IDENTITY")
    tickets = {}
    for market, row in page.rows:
        selections = [c["text"].strip() for c in row if c["class"] in {"a", "d"}]
        amounts = [c["text"].strip() for c in row if c["class"] == "refundMoney"]
        if (len(selections) != 1 or len(amounts) != 1
                or not re.fullmatch(r"[0-9]+(?:-[0-9]+)*", selections[0])
                or not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)円", amounts[0])):
            raise ValueError("PAYOUT_EXCEPTION_OR_SCHEMA")
        selection = tuple(map(int, selections[0].split("-")))
        limit = 8 if market.startswith("bracket") else 16
        if len(selection) != ARITY[market] or any(not 1 <= n <= limit for n in selection):
            raise ValueError("PAYOUT_SELECTION")
        if not market.startswith("bracket") and len(set(selection)) != len(selection):
            raise ValueError("PAYOUT_SELECTION")
        if market in UNORDERED:
            selection = tuple(sorted(selection))
        key = (market, "-".join(map(str, selection)))
        amount = int(amounts[0][:-1].replace(",", ""))
        if key in tickets or amount < 1:
            raise ValueError("PAYOUT_DUPLICATE_OR_AMOUNT")
        tickets[key] = amount
    if not tickets:
        raise ValueError("PAYOUT_EMPTY")
    return tickets


def crosscheck(store, raw, filename, html, race_id, encoding):
    from .realdata import RealData

    report = {"version": VERSION, "race_id": race_id, "paper_eligible": False,
              "settlement_eligible": False, "exceptions_qualified": False,
              "interpretation": "PAYOUT_CROSSCHECK_NOT_SETTLEMENT",
              "html_raw_hash": store.body(html, "raw")}
    inspection = RealData(store).inspect(raw, filename, "DAILY_SNAPSHOT", encoding)
    report.update(csv_raw_hash=inspection["raw_hash"], inspection_id=inspection["id"])
    try:
        if inspection.get("content_type") != "race" or inspection["status"] != "PARSED_UNQUALIFIED":
            raise ValueError("PAYOUT_CSV_SCHEMA")
        race = inspection["content"]["races"].get(race_id)
        if race is None:
            raise ValueError("PAYOUT_RACE_IDENTITY")
        parsed = parse_result_page(html, race_id)
        csv = {(r["market"], r["selection"]): r["payout_per_100"] for r in race["payout_tickets"]}
        report.update(status="MATCHED_UNQUALIFIED" if parsed == csv else "MISMATCH",
                      html_tickets=[{"market": m, "selection": s, "payout_per_100": v}
                                    for (m, s), v in parsed.items()], csv_tickets=race["payout_tickets"])
    except (ValueError, UnicodeError):
        report.update(status="QUARANTINED", reason="PAYOUT_PAGE_OR_CSV_UNQUALIFIED")
    report["checked_at"] = stamp(store.clock())
    return report
