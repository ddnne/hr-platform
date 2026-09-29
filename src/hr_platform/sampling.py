"""Finite local research captures. No scheduler, redirects or automatic retries.

All kinds share one Store's spacing and refusal state. Use the same private root
for every local sample; this never changes the Cloudflare collector's stop state.
"""

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import os
import re
import time
import urllib.error
import urllib.request
from .common import canonical, identity, instant, seconds, stamp
from .parser import MAX_COMPRESSED, VERSION
from .race_metadata import MetadataEvidence
from .race_state import StateEvidence, MAX_BYTES, validate_page_url
from .realdata import filename_metadata

INTERVAL = 120
FORMAT = "nar-finite-local-v1"
ZIP_URLS = {k: "https://www.keiba.go.jp/KeibaWeb/DataDownload/" + v + "?type=daily"
            for k, v in {"odds": "OddsDataDownload", "race": "RaceDataDownload"}.items()}
PAGE_PATHS = {"state": "/KeibaWeb/TodayRaceInfo/OddsTanFuku",
              "payout": "/KeibaWeb/TodayRaceInfo/RaceMarkTable"}
HEADERS = ("Content-Type", "Date", "Retry-After", "cf-mitigated", "Content-Disposition")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_plans(id TEXT PRIMARY KEY, registered_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sample_requests(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
 body TEXT NOT NULL, receipt_hash TEXT);
CREATE TABLE IF NOT EXISTS sample_control(id INTEGER PRIMARY KEY CHECK(id=1),
 stopped INTEGER NOT NULL, next_at TEXT);
INSERT OR IGNORE INTO sample_control VALUES(1,0,NULL);
"""


def enabled():
    if any(os.environ.get(k, "").lower() in {"true", "1"} for k in ("CI", "GITHUB_ACTIONS")):
        raise ValueError("REAL_DATA_DISABLED_IN_CI")


def validate_plan(plan):
    if set(plan) != {"format", "requests"} or plan["format"] != FORMAT:
        raise ValueError("SAMPLE_PLAN")
    items = plan["requests"]
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError("SAMPLE_PLAN_LIMIT")
    names, times = set(), []
    for item in items:
        if set(item) != {"id", "kind", "scope", "url", "at", "until"}:
            raise ValueError("SAMPLE_ITEM")
        if not re.fullmatch(r"[a-z0-9-]{1,40}", item["id"]) or item["id"] in names:
            raise ValueError("SAMPLE_ITEM_ID")
        names.add(item["id"])
        kind = item["kind"]
        if kind in ZIP_URLS:
            if item["url"] != ZIP_URLS[kind] or not re.fullmatch(r"\d{8}", item["scope"]):
                raise ValueError("SAMPLE_URL")
            datetime.strptime(item["scope"], "%Y%m%d")
        elif kind in PAGE_PATHS:
            validate_page_url(item["url"], item["scope"], PAGE_PATHS[kind])
        else:
            raise ValueError("SAMPLE_KIND")
        at, until = stamp(item["at"]), stamp(item["until"])
        if not 0 < seconds(until, at) <= INTERVAL:
            raise ValueError("SAMPLE_WINDOW")
        times.append(at)
    if times != sorted(times) or any(seconds(b, a) < INTERVAL for a, b in zip(times, times[1:])):
        raise ValueError("SAMPLE_SPACING")
    if seconds(times[-1], times[0]) > 86400:
        raise ValueError("SAMPLE_WINDOW")
    return identity(plan)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def open_response(item):
    """The only native provider request entry point; no cookies or auth state."""
    enabled()
    request = urllib.request.Request(item["url"], headers={
        "User-Agent": "hr-platform-personal-research/0.1",
        "Accept": "application/zip" if item["kind"] in ZIP_URLS else "text/html",
    })
    try:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as response:
        return response


def after(now, value):
    minimum = instant(now) + timedelta(seconds=INTERVAL)
    try:
        at = (instant(now) + timedelta(seconds=int(value))) if str(value).strip().isdigit() else parsedate_to_datetime(value)
        return stamp(max(minimum, at).isoformat())
    except (ValueError, TypeError, OverflowError):
        return stamp(minimum.isoformat())


class Samples:
    def __init__(self, store, opener=open_response):
        self.store, self.opener = store, opener
        store.db.executescript(SCHEMA)

    def register(self, plan):
        key = validate_plan(plan)
        previous = self.store.db.execute("SELECT id FROM sample_plans WHERE id=?", (key,)).fetchone()
        if not previous:
            now = stamp(self.store.clock())
            if now > stamp(plan["requests"][0]["at"]):
                raise ValueError("SAMPLE_PLAN_TOO_LATE")
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO sample_plans VALUES(?,?,?)", (key, now, canonical(plan).decode()))
        # Repair a partial registration too; no HTTP occurs during this step.
        self.store.plan([item["at"] for item in plan["requests"] if item["kind"] == "odds"])
        return key

    def control(self, *, stop=False, next_at=None):
        with self.store.db:
            self.store.db.execute("""UPDATE sample_control SET stopped=max(stopped,?),
                next_at=CASE WHEN ? IS NULL THEN next_at ELSE max(coalesce(next_at,''),?) END WHERE id=1""",
                (int(stop), next_at, next_at))

    def capture(self, plan, name):
        enabled()
        plan_id = self.register(plan)
        item = next((x for x in plan["requests"] if x["id"] == name), None)
        if item is None:
            raise ValueError("SAMPLE_ITEM_ID")
        key = identity([plan_id, name])
        reserved = self.reserve(plan_id, key, item)
        if reserved["status"] == "REPLAY":
            receipt = json.loads(self.store.read_body(reserved["receipt_hash"], "receipts"))
            return self.publish(item, receipt)
        if reserved["status"] != "RESERVED":
            return reserved
        now = reserved["reserved_at"]
        started = stamp(self.store.clock())
        self.control(next_at=after(started, None))
        receipt = {"event_id": key, "url": item["url"], "kind": item["kind"],
                   "scheduled_capture_at": stamp(item["at"]), "fetch_started_at": started,
                   "source_updated_at": None, "scope": "FINITE_LOCAL_SAMPLE",
                   "status": None, "accepted": False, "http_attempted": False, "outcome": "SAMPLE_WINDOW_EXPIRED"}
        if started < now or started >= stamp(item["until"]):
            return self.finish(item, receipt)
        return self.request(item, receipt)

    def reserve(self, plan_id, key, item):
        # Serialize only the short reservation. Concurrent invocations cannot
        # both pass spacing/refusal checks before either attempt is recorded.
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            old = self.store.db.execute("SELECT * FROM sample_requests WHERE id=?", (key,)).fetchone()
            if old:
                if old["receipt_hash"]:
                    return {"status": "REPLAY", "receipt_hash": old["receipt_hash"]}
                return {"status": "INCOMPLETE_ATTEMPT_STOP", "attempt_id": key}
            now = stamp(self.store.clock())
            control = self.store.db.execute("SELECT * FROM sample_control WHERE id=1").fetchone()
            if self.store.db.execute("SELECT 1 FROM sample_requests WHERE receipt_hash IS NULL").fetchone():
                return {"status": "INCOMPLETE_ATTEMPT_STOP"}
            if control["stopped"]:
                return {"status": "SOURCE_STOPPED"}
            if now < stamp(item["at"]) or control["next_at"] and now < control["next_at"]:
                return {"status": "WAIT", "next_at": max(stamp(item["at"]), control["next_at"] or "")}
            if now >= stamp(item["until"]):
                return {"status": "SAMPLE_WINDOW_EXPIRED"}
            self.store.db.execute("INSERT INTO sample_requests VALUES(?,?,?,NULL)",
                                  (key, plan_id, canonical(item).decode()))
            return {"status": "RESERVED", "reserved_at": now}

    def request(self, item, receipt):
        # Recheck after reservation/control writes; their latency must not extend
        # a finite capture window. Preserve the actual request start separately.
        actual = stamp(self.store.clock())
        if actual < receipt["fetch_started_at"] or actual >= stamp(item["until"]):
            return self.finish(item, receipt)
        receipt["fetch_started_at"] = actual
        begin = time.monotonic()
        response = None
        try:
            receipt["http_attempted"] = True
            response = self.opener(item)
            receipt.update(headers_received_at=stamp(self.store.clock()), status=response.status,
                           headers={k: response.headers.get(k) for k in HEADERS})
            status, headers = receipt["status"], receipt["headers"]
            denied = status not in {200, 429} or headers["cf-mitigated"] == "challenge"
            if denied:
                self.control(stop=True)
            if status == 429:
                self.control(next_at=after(receipt["headers_received_at"], headers["Retry-After"]))
            limit = (MAX_COMPRESSED if item["kind"] in ZIP_URLS else MAX_BYTES) if status == 200 else 8192
            chunks, size = [], 0
            while size <= limit:
                if time.monotonic() - begin > 30:
                    raise TimeoutError("SAMPLE_TIMEOUT")
                chunk = response.read(min(64 * 1024, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            receipt.update(collector_received_at=stamp(self.store.clock()), bytes=len(raw), body_truncated=size > limit)
            digest = self.store.body(raw, "raw")
            receipt.update(sha256=digest, raw_saved_at=stamp(self.store.clock()))
            challenge = bool(re.search(rb"captcha|challenge", raw, re.I))
            if denied or challenge:
                self.control(stop=True)
                receipt["outcome"] = "SOURCE_DENIED"
            elif size > limit:
                self.control(stop=True)
                receipt["outcome"] = "BODY_LIMIT"
            elif status == 429:
                receipt["outcome"] = "RATE_LIMITED"
            elif item["kind"] in ZIP_URLS:
                match = re.search(r"(\d{8}_\d{10}_" + item["kind"] + r"\.zip)", headers["Content-Disposition"] or "")
                receipt["filename"] = match[1] if match else None
                receipt["accepted"] = bool(match and match[1].startswith(item["scope"] + "_") and raw.startswith(b"PK\x03\x04"))
                receipt["outcome"] = "RAW_STORED" if receipt["accepted"] else "BODY_UNQUALIFIED"
            else:
                receipt["accepted"] = "text/html" in (headers["Content-Type"] or "").lower()
                receipt["outcome"] = "RAW_STORED" if receipt["accepted"] else "BODY_UNQUALIFIED"
            if receipt["outcome"] == "BODY_UNQUALIFIED":
                self.control(stop=True)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # Preserve already-written stop/wait state even when reading or closing fails.
            receipt.update(outcome="REQUEST_OR_STORAGE_ERROR", error_class=type(exc).__name__)
            self.control(stop=True)
        finally:
            if response:
                try:
                    response.close()
                except OSError:
                    pass
            # Waiting from completion is conservative and also covers a slow
            # reservation or network call. Retry-After is never shortened.
            self.control(next_at=after(stamp(self.store.clock()), None))
        receipt["duration_ms"] = (time.monotonic() - begin) * 1000
        return self.finish(item, receipt)

    def finish(self, item, receipt):
        receipt["recorded_at"] = stamp(self.store.clock())
        digest = self.store.body(canonical(receipt), "receipts")
        with self.store.db:
            self.store.db.execute("UPDATE sample_requests SET receipt_hash=? WHERE id=?",
                                  (digest, receipt["event_id"]))
        return self.publish(item, receipt)

    def publish(self, item, receipt):
        result = {"status": receipt["outcome"], "attempt_id": receipt["event_id"],
                  "receipt_hash": identity(receipt), "http_status": receipt["status"]}
        raw = self.store.read_body(receipt["sha256"], "raw") if receipt["accepted"] else None
        if item["kind"] == "odds" and receipt["http_attempted"]:
            prior = self.store.db.execute("SELECT event FROM attempts WHERE id=?", (receipt["event_id"],)).fetchone()
            ingested = json.loads(prior[0])["ingest_received_at"] if prior else stamp(self.store.clock())
            event = {"attempt_id": receipt["event_id"], "scheduled_capture_at": receipt["scheduled_capture_at"],
                     "fetch_started_at": receipt["fetch_started_at"], "collector_received_at": receipt.get("collector_received_at"),
                     "ingest_received_at": ingested, "collector_receipt_recorded_at": receipt["recorded_at"], "status": receipt["status"],
                     "response_accepted": receipt["accepted"], "dataset_kind": "DAILY_SNAPSHOT",
                     "source": "nar-local-finite-sample", "availability_basis": "LOCAL_COLLECTOR_RECEIPT",
                     "receipt_hash": identity(receipt), "collector_raw_saved_at": receipt.get("raw_saved_at"),
                     "headers_received_at": receipt.get("headers_received_at"), "encoding": "utf-8-sig",
                     "file_name": receipt.get("filename"), "file_timestamp": filename_metadata(receipt.get("filename"))["file_timestamp"],
                     "diagnostic_raw_hash": receipt.get("sha256") if not receipt["accepted"] else None,
                     "source_updated_at": None, "source_published_at": None, "race_states": {}, "etag": None, "retry_of": None}
            parsed = self.store.ingest(event, raw, VERSION + ":utf-8-sig")
            if not receipt["accepted"]:
                return result
            report = dict(self.store.db.execute("SELECT * FROM parses WHERE id=?", (parsed,)).fetchone())
        else:
            if not receipt["accepted"]:
                return result
            if item["kind"] == "payout":
                from .official_payout import PayoutEvidence

                cls = PayoutEvidence
            else:
                cls = MetadataEvidence if item["kind"] == "race" else StateEvidence
            report = cls(self.store).ingest(receipt, raw, item["scope"])
        return {**result, "status": "QUARANTINED" if report["status"] in {"ERROR", "QUARANTINED"} else "PARSED",
                "parse": report}
