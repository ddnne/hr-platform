"""Published ordinary quinella/trio payouts and horse-number exclusion refunds.

The result table is a consistency check; monetary payouts always come from the
official payout table. Frame bets, ties, special payouts and void races are not
qualified by this adapter. Result evidence never supplies pre-race state.
"""

import json
import re
from itertools import combinations
from urllib.parse import parse_qs, urlsplit
from .common import stamp
from .paper import settle
from .payout_check import ResultPage, parse_result_page
from .race_state import StateEvidence, compact

VERSION = "nar-result-payout-v2"
RULE_SOURCE = "https://www.keiba.go.jp/beginner/step6.html"
STATUS_SOURCE = "https://www.keiba.go.jp/beginner/step2.html"
HEADERS = [
    "着順",
    "枠",
    "馬番",
    "馬名",
    "所属",
    "性齢",
    "負担重量",
    "騎手(所属)",
    "調教師",
    "馬体重(増減)",
    "タイム",
    "着差",
    "上がり3F",
    "コーナー通過順",
    "人気",
    "単勝オッズ",
]
VOID_TAGS = {
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
}


class FinishPage(ResultPage):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.grade = False
        self.grade_sections = self.grade_tables = 0
        self.grade_table = False
        self.grade_row = self.grade_cell = None
        self.grade_rows = []
        self.race_links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = compact(a.get("style", "")).lower()
        hidden = (
            any(x[1] for x in self.elements)
            or "hidden" in a
            or a.get("aria-hidden", "").lower() == "true"
            or bool(
                re.search(
                    r"(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)", style
                )
            )
            or tag in {"script", "style", "template"}
        )
        protected_section = tag == "section" and bool(
            {"gradeTable", "newRefundTable"} & set(a.get("class", "").split())
        )
        race_link = tag == "a" and a.get("id") == "RaceList"
        if hidden and (
            self.grade
            or self.depth
            or protected_section
            or race_link
            or tag == "h4"
            or self.heading is not None
        ):
            raise ValueError("PAYOUT_HIDDEN_EVIDENCE")
        if tag not in VOID_TAGS:
            self.elements.append((tag, hidden))
        if race_link:
            self.race_links.append(a.get("href", ""))
        if tag == "section" and self.grade:
            raise ValueError("PAYOUT_GRADE_STRUCTURE")
        if tag == "section" and "gradeTable" in a.get("class", "").split():
            self.grade = True
            self.grade_sections += 1
        if self.grade:
            if tag == "table":
                if self.grade_table:
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                self.grade_tables += 1
                self.grade_table = True
            if tag == "tr":
                if not self.grade_table or self.grade_row is not None:
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                self.grade_row = []
            if tag in {"th", "td"}:
                if self.grade_row is None or self.grade_cell is not None:
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                if a.get("rowspan", "1") != "1" or a.get("colspan", "1") != "1":
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                self.grade_cell = {"tag": tag, "text": ""}
        super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.grade_cell is not None:
            self.grade_cell["text"] += data
        super().handle_data(data)

    def handle_endtag(self, tag):
        if self.grade:
            if tag in {"th", "td"}:
                if self.grade_cell is None or self.grade_cell["tag"] != tag:
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                self.grade_row.append(self.grade_cell)
                self.grade_cell = None
            if tag == "tr":
                if self.grade_row is None or self.grade_cell is not None:
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                self.grade_rows.append(self.grade_row)
                self.grade_row = None
            if tag in {"table", "section"}:
                if (
                    self.grade_row is not None
                    or self.grade_cell is not None
                    or not self.grade_table
                    and tag == "table"
                ):
                    raise ValueError("PAYOUT_GRADE_STRUCTURE")
                if tag == "table":
                    self.grade_table = False
                else:
                    if self.grade_table:
                        raise ValueError("PAYOUT_GRADE_STRUCTURE")
                    self.grade = False
        super().handle_endtag(tag)
        matching = [i for i, x in enumerate(self.elements) if x[0] == tag]
        if matching:
            del self.elements[matching[-1] :]


def parse_payout_page(raw, race_id):
    page = FinishPage()
    page.feed(raw.decode("utf-8-sig", errors="strict"))
    if (
        page.grade_sections != 1
        or page.grade_tables != 1
        or page.grade
        or page.grade_table
        or page.grade_row is not None
        or page.grade_cell is not None
        or len(page.grade_rows) < 4
    ):
        raise ValueError("PAYOUT_GRADE_STRUCTURE")
    header = page.grade_rows[0]
    # Some published results omit the corner-order column. It is not used for
    # finish positions or payments; keep every other column and row width exact.
    labels = [compact(c["text"]) for c in header]
    without_corners = [h for h in HEADERS if h != "コーナー通過順"]
    if labels not in (HEADERS, without_corners) or any(c["tag"] != "th" for c in header):
        raise ValueError("PAYOUT_GRADE_HEADER")
    if len(page.race_links) != 1:
        raise ValueError("PAYOUT_RACE_LINK")
    link = urlsplit(page.race_links[0])
    query = parse_qs(link.query, strict_parsing=True)
    date, _, number = race_id.split(":")
    if (
        link.scheme
        or link.netloc
        or link.fragment
        or link.path != "../TodayRaceInfo/RaceList"
        or set(query) != {"k_raceDate", "k_raceNo", "k_babaCode"}
        or query["k_raceDate"] != [f"{date[:4]}/{date[4:6]}/{date[6:]}"]
        or query["k_raceNo"] != [number]
        or len(query["k_babaCode"]) != 1
        or not re.fullmatch(r"[0-9]{2}", query["k_babaCode"][0])
    ):
        raise ValueError("PAYOUT_RACE_LINK")
    runners, ranks = {}, {}
    for row in page.grade_rows[1:]:
        if len(row) != len(header) or any(c["tag"] != "td" for c in row):
            raise ValueError("PAYOUT_GRADE_ROW")
        rank, horse = compact(row[0]["text"]), compact(row[2]["text"])
        if not re.fullmatch(r"[1-9][0-9]?", horse) or not 1 <= int(horse) <= 16 or horse in runners:
            raise ValueError("PAYOUT_HORSE_ID")
        statuses = {"除外": "EXCLUDED", "取消": "CANCELLED_BEFORE_SALES", "中止": "DID_NOT_FINISH"}
        if re.fullmatch(r"[1-9][0-9]?", rank) and 1 <= int(rank) <= 16:
            if int(rank) in ranks:
                raise ValueError("PAYOUT_TIE_UNQUALIFIED")
            ranks[int(rank)] = int(horse)
            status = "FINISHED"
        elif rank in statuses:
            status = statuses[rank]
        else:
            raise ValueError("PAYOUT_FINISH_UNQUALIFIED")
        runners[horse] = {"finish_label": rank, "status": status}
    if set(map(int, runners)) != set(range(1, len(runners) + 1)) or set(ranks) != set(
        range(1, len(ranks) + 1)
    ):
        raise ValueError("PAYOUT_INCOMPLETE_ROSTER_OR_RANKS")
    published = parse_result_page(raw, race_id)
    tickets, complete = [], []
    for market, arity in (("quinella", 2), ("trio", 3)):
        winning = [(s, v) for (m, s), v in published.items() if m == market]
        if not winning:
            continue
        if (
            len(ranks) < arity
            or len(winning) != 1
            or winning[0][0] != "-".join(map(str, sorted(ranks[i] for i in range(1, arity + 1))))
        ):
            raise ValueError("PAYOUT_RESULT_CONFLICT_OR_TIE")
        complete.append(market)
        tickets.append({"market": market, "selection": winning[0][0], "payout_per_100": winning[0][1]})
        sold = sorted(int(h) for h, row in runners.items() if row["status"] != "CANCELLED_BEFORE_SALES")
        for selection in combinations(sold, arity):
            if any(runners[str(h)]["status"] == "EXCLUDED" for h in selection):
                tickets.append(
                    {"market": market, "selection": "-".join(map(str, selection)), "refund_per_100": 100}
                )
    if not complete:
        raise ValueError("PAYOUT_TARGET_NOT_PUBLISHED")
    return {
        "race_id": race_id,
        "venue_code": query["k_babaCode"][0],
        "runners": runners,
        "status": "PAYOUT_QUALIFIED",
        "paper_eligible": False,
        "source_kind": "OFFICIAL",
        "final": True,
        "complete_markets": complete,
        "tickets": tickets,
        "void": False,
        "source_updated_at": None,
        "finality_basis": "OFFICIAL_PUBLISHED_PAYOUT",
        "refund_basis": "EXCLUDED_HORSE_COMBINATIONS",
        "rule_sources": [RULE_SOURCE, STATUS_SOURCE],
        "scope": "ORDINARY_QUINELLA_TRIO_WITHOUT_TIES",
    }


class PayoutEvidence(StateEvidence):
    table_prefix = "official_payout"
    receipt_path = "/KeibaWeb/TodayRaceInfo/RaceMarkTable"
    version = VERSION
    parser = staticmethod(parse_payout_page)

    def settle_decision(self, decision_id, evidence_id):
        # Only recorded, already published evidence may reach the ledger. There
        # is no path here to create a decision or backdate an import.
        row = self.store.db.execute(
            "SELECT available_at FROM official_payout_parses WHERE id=?", (evidence_id,)
        ).fetchone()
        if row is None or row["available_at"] is None or row["available_at"] > stamp(self.store.clock()):
            raise ValueError("PAYOUT_EVIDENCE_UNAVAILABLE")
        evidence = self.published(evidence_id)
        if evidence["status"] != "PAYOUT_QUALIFIED":
            raise ValueError("PAYOUT_EVIDENCE_UNQUALIFIED")
        decision_row = self.store.db.execute(
            "SELECT body FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if decision_row is None:
            raise ValueError("PAYOUT_DECISION_MISSING")
        decision = json.loads(decision_row[0])
        if decision["stake_yen"]:
            if decision["target"] not in evidence["complete_markets"]:
                raise ValueError("PAYOUT_MARKET_UNQUALIFIED")
            selection = decision["selection"].split("-")
            if any(
                h not in evidence["runners"] or evidence["runners"][h]["status"] == "CANCELLED_BEFORE_SALES"
                for h in selection
            ):
                raise ValueError("PAYOUT_SELECTION_NOT_SOLD")
        return settle(
            self.store,
            decision_id,
            {
                **evidence,
                "revision": evidence["id"],
                "source_reference": {
                    "raw_hash": evidence["raw_hash"],
                    "receipt_hash": evidence["receipt_hash"],
                    "version": evidence["version"],
                    "rules": evidence["rule_sources"],
                },
            },
        )
