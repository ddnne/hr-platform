"""Private D1 capture-log snapshots, separate from market observations.

An exported status is known locally only after import. A later repair or a late
export never rewrites an earlier snapshot. Logs alone cannot create odds or bets.
"""

from datetime import timedelta
import json
import math
import re
from .common import canonical, identity, instant, stamp

SOURCE = "nar-daily-odds"
FORMAT = "hr-capture-log-v1"
MAX_ROWS = 10000
FIELDS = (
    "event_id scheduled_capture_at fetch_started_at status http_status headers_received_at "
    "collector_received_at raw_saved_at raw_sha256 raw_bytes etag validator_sent "
    "validator_raw_sha256 file_name file_timestamp duration_ms retry_after_at "
    "parsed_at available_at error_code processing_ms"
).split()
SCHEMA = """
CREATE TABLE IF NOT EXISTS collector_exports(
 id TEXT PRIMARY KEY, read_started_at TEXT NOT NULL, read_completed_at TEXT NOT NULL,
 range_start TEXT NOT NULL, range_end TEXT NOT NULL, imported_at TEXT);
CREATE TABLE IF NOT EXISTS collector_log_versions(
 export_id TEXT NOT NULL REFERENCES collector_exports(id), event_id TEXT NOT NULL,
 scheduled_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(export_id,event_id));
CREATE INDEX IF NOT EXISTS collector_log_events ON collector_log_versions(event_id);
"""


def window(start, end):
    start, end = stamp(start), stamp(end)
    if not 0 < (instant(end) - instant(start)).total_seconds() <= 86400:
        raise ValueError("LOG_WINDOW_LIMIT")
    return start, end


def validate_export(data, now):
    if not isinstance(data, dict) or set(data) != {
        "format",
        "source",
        "read_started_at",
        "read_completed_at",
        "range_start",
        "range_end",
        "rows",
    }:
        raise ValueError("LOG_SCHEMA")
    if data["format"] != FORMAT or data["source"] != SOURCE:
        raise ValueError("LOG_SOURCE")
    start, end = window(data["range_start"], data["range_end"])
    read_start, read_end = stamp(data["read_started_at"]), stamp(data["read_completed_at"])
    if not read_start <= read_end <= stamp(now) or end > read_start:
        raise ValueError("LOG_TIME")
    rows = data["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("LOG_ROW_LIMIT")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("LOG_ROW_SCHEMA")
        event = row["event_id"]
        if not isinstance(event, str) or not re.fullmatch(SOURCE + r":\d+", event) or event in ids:
            raise ValueError("LOG_EVENT")
        ids.add(event)
        slot = stamp(row["scheduled_capture_at"])
        if not start <= slot < end or int(instant(slot).timestamp() * 1000) != int(event.split(":")[1]):
            raise ValueError("LOG_SLOT")
        if row["status"] not in {"PENDING", "WAIT_OR_BLOCKED", "FAILED", "STORAGE_ERROR", "RAW_STORED"}:
            raise ValueError("LOG_STATUS")
        prior = slot
        for field in ("fetch_started_at", "headers_received_at", "collector_received_at", "raw_saved_at"):
            if row[field] is not None:
                value = stamp(row[field])
                if not prior <= value <= read_end:
                    raise ValueError("LOG_ROW_TIME")
                prior = value
        for field in ("retry_after_at", "file_timestamp", "parsed_at", "available_at"):
            if row[field] is not None:
                stamp(row[field])  # retry/file time is not a receipt/availability timestamp
        for field in ("duration_ms", "processing_ms"):
            value = row[field]
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("LOG_METRIC")
        if row["http_status"] is not None and (
            type(row["http_status"]) is not int or not 100 <= row["http_status"] <= 599
        ):
            raise ValueError("LOG_HTTP")
        if row["raw_bytes"] is not None and (type(row["raw_bytes"]) is not int or row["raw_bytes"] < 0):
            raise ValueError("LOG_SIZE")
        for field in ("raw_sha256", "validator_raw_sha256"):
            if row[field] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(row[field])):
                raise ValueError("LOG_HASH")
        for field in ("etag", "validator_sent", "file_name", "error_code"):
            if row[field] is not None and (not isinstance(row[field], str) or len(row[field]) > 1024):
                raise ValueError("LOG_TEXT")
        if row["status"] == "WAIT_OR_BLOCKED" and any(row[k] is not None for k in FIELDS[4:]):
            raise ValueError("LOG_WAIT_RECEIPT")
        if row["status"] == "WAIT_OR_BLOCKED" and row["fetch_started_at"] is not None:
            raise ValueError("LOG_WAIT_RECEIPT")
        if row["status"] != "WAIT_OR_BLOCKED" and row["fetch_started_at"] is None:
            raise ValueError("LOG_START_MISSING")
        if row["status"] == "RAW_STORED" and (
            row["http_status"] not in (200, 304)
            or any(
                row[k] is None
                for k in (
                    "headers_received_at",
                    "collector_received_at",
                    "raw_saved_at",
                    "raw_sha256",
                    "raw_bytes",
                )
            )
            or row["error_code"] is not None
        ):
            raise ValueError("LOG_RAW_RECEIPT")
        if (
            row["status"] == "RAW_STORED"
            and row["http_status"] == 304
            and (
                not row["validator_sent"]
                or row["etag"] != row["validator_sent"]
                or row["raw_sha256"] != row["validator_raw_sha256"]
            )
        ):
            raise ValueError("LOG_UNBOUND_304")
    return start, end, read_start, read_end


class CaptureLog:
    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)

    def ingest(self, data):
        now = stamp(self.store.clock())
        start, end, read_start, read_end = validate_export(data, now)
        digest = identity(data)
        existing = self.store.db.execute(
            "SELECT imported_at FROM collector_exports WHERE id=?", (digest,)
        ).fetchone()
        if not existing:
            self.store.body(canonical(data), "receipts")
            # Publish all rows atomically. No synthesized receive time, attempt or observation.
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO collector_exports VALUES(?,?,?,?,?,NULL)",
                    (digest, read_start, read_end, start, end),
                )
                self.store.db.executemany(
                    "INSERT INTO collector_log_versions VALUES(?,?,?,?)",
                    [
                        (digest, r["event_id"], stamp(r["scheduled_capture_at"]), canonical(r).decode())
                        for r in data["rows"]
                    ],
                )
        if not existing or existing[0] is None:
            available = stamp(self.store.clock())
            if available < now:
                raise ValueError("LOG_CLOCK_ORDER")
            with self.store.db:
                self.store.db.execute(
                    "UPDATE collector_exports SET imported_at=? WHERE id=? AND imported_at IS NULL",
                    (available, digest),
                )
        available = self.store.db.execute(
            "SELECT imported_at FROM collector_exports WHERE id=?", (digest,)
        ).fetchone()[0]
        return {
            "status": "LOG_IMPORTED",
            "export_id": digest,
            "rows": len(data["rows"]),
            "available_at": available,
        }

    def history(self, start, end, at):
        start, end = window(start, end)
        at = stamp(at)
        rows = self.store.db.execute(
            """SELECT v.*,e.read_started_at,e.read_completed_at,e.imported_at FROM collector_log_versions v
            JOIN collector_exports e ON e.id=v.export_id
            WHERE v.scheduled_at>=? AND v.scheduled_at<? AND e.imported_at<=?
            ORDER BY v.scheduled_at,e.read_completed_at,e.imported_at,e.id""",
            (start, end, at),
        ).fetchall()
        events = {}
        for row in rows:
            record = dict(row)
            record["capture"] = json.loads(record.pop("body"))
            events.setdefault(row["event_id"], []).append(record)
        result = []
        for event, revisions in events.items():
            # Only non-overlapping reads establish order. CLI completion order is
            # not SQL snapshot order when two reads overlap.
            candidates = [
                r
                for r in revisions
                if not any(r["read_completed_at"] < other["read_started_at"] for other in revisions)
            ]
            uncertain = len({identity(r["capture"]) for r in candidates}) > 1
            latest = None if uncertain else candidates[-1]
            capture = latest["capture"] if latest else None
            parsed = self.store.db.execute(
                """SELECT p.id,p.available_at,o.raw_hash FROM parses p JOIN observations o ON o.id=p.observation_id
                WHERE o.id=? AND p.available_at<=? ORDER BY p.available_at DESC,p.id DESC LIMIT 1""",
                (event, at),
            ).fetchone()
            receipt = self.store.db.execute("SELECT event FROM attempts WHERE id=?", (event,)).fetchone()
            proven = (
                receipt is not None
                and json.loads(receipt[0]).get("availability_basis") == "EXPORTED_COLLECTOR_RECEIPT"
            )
            matching = (
                parsed is not None
                and proven
                and capture is not None
                and parsed["raw_hash"] == capture["raw_sha256"]
            )
            result.append(
                {
                    "event_id": event,
                    "scheduled_at": revisions[0]["scheduled_at"],
                    "current": latest,
                    "current_status": "ORDER_UNCERTAIN" if uncertain else capture["status"],
                    "order_uncertain": uncertain,
                    "current_candidates": candidates,
                    "revisions": revisions,
                    "local_parse": dict(parsed) if matching else None,
                    "local_parse_available": matching,
                    "raw_hash_conflict": parsed is not None
                    and capture is not None
                    and parsed["raw_hash"] != capture["raw_sha256"],
                }
            )
        # This is an audit grid, not evidence a Cron schedule was actually enabled.
        slots = []
        t = instant(start)
        while t < instant(end):
            next_at = min(t + timedelta(seconds=120), instant(end))
            records = [e for e in result if t <= instant(e["scheduled_at"]) < next_at]
            slots.append(
                {
                    "slot": stamp(t.isoformat()),
                    "until": stamp(next_at.isoformat()),
                    "event_ids": [e["event_id"] for e in records],
                    "status": records[0]["current_status"]
                    if len(records) == 1
                    else "MULTIPLE_RECORDS"
                    if records
                    else "NO_CAPTURE_RECORD",
                }
            )
            t = next_at
        return {
            "asof_at": at,
            "range_start": start,
            "range_end": end,
            "events": result,
            "audit_slots": slots,
            "capture_plan": "UNVERIFIED",
            "scheduled_coverage": None,
            "note": "NO_CAPTURE_RECORDは未実行の断定ではない。取得計画未接続。ログだけではオッズを利用可能にしない。",
        }
