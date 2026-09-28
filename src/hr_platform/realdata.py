"""Private, offline qualification and import. This module never downloads from NAR.

An unaccompanied ZIP is an inspected asset, not a historical observation. Only a
validated collector receipt creates an observation, with availability assigned now.
"""

import csv
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import re
import time
from .common import canonical, identity, instant, sha, stamp
from .parser import parse_odds, unzip, VERSION, MAX_COMPRESSED
from .race_files import parse_race_bundle, VERSION as RACE_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_inspections(
 id TEXT PRIMARY KEY, raw_hash TEXT NOT NULL, raw_saved_at TEXT NOT NULL,
 inspected_at TEXT NOT NULL, report_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_imports(
 event_id TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL, raw_hash TEXT NOT NULL,
 imported_at TEXT NOT NULL, encoding TEXT NOT NULL);
"""
MANIFEST_FIELDS = {
    "event_id",
    "scheduled_capture_at",
    "fetch_started_at",
    "headers_received_at",
    "collector_received_at",
    "raw_saved_at",
    "raw_sha256",
    "raw_bytes",
    "http_status",
    "etag",
    "validator_sent",
    "validator_raw_sha256",
    "file_name",
    "file_timestamp",
    "duration_ms",
}


def read_limited(path, limit=MAX_COMPRESSED):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("INPUT_LIMIT")
    return data


def filename_metadata(filename):
    if filename is None:
        return {"filename_kind": None, "file_timestamp": None}
    match = re.fullmatch(r"(\d{6}|\d{8})_(\d{10})_(odds|race)\.zip", filename)
    if not match:
        return {"filename_kind": "UNRECOGNIZED", "file_timestamp": None}
    date, seconds, _ = match.groups()
    datetime.strptime(date, "%Y%m%d" if len(date) == 8 else "%Y%m")
    return {
        "filename_kind": "DAILY_SNAPSHOT" if len(date) == 8 else "FINAL_ONLY",
        "file_timestamp": datetime.fromtimestamp(int(seconds), timezone.utc).isoformat(),
    }


def completeness(odds, race_bundle=None):
    """Entry-based coverage is a diagnostic, never evidence that runners are active."""
    result = {}
    for race_id, race in odds.items():
        entries = (race_bundle or {}).get("races", {}).get(race_id, {}).get("horses", {})
        horses = sorted(map(int, entries))
        markets = {}
        for market, body in race["markets"].items():
            actual = set(body["quotes"])
            expected = None
            if horses:
                if market in {"win", "place"}:
                    combinations = [(h,) for h in horses]
                elif market in {"quinella", "wide", "trio"}:
                    combinations = itertools.combinations(horses, 3 if market == "trio" else 2)
                elif market in {"exacta", "trifecta"}:
                    combinations = itertools.permutations(horses, 3 if market == "trifecta" else 2)
                else:
                    combinations = None  # bracket combinations require qualified frame/runner rules
                if combinations is not None:
                    expected = {"-".join(map(str, s)) for s in combinations}
            markets[market] = {
                "rows": len(actual),
                "fixed_numeric_rows": sum(q["display_status"] == "FIXED" for q in body["quotes"].values()),
                "expected_rows": len(expected) if expected is not None else None,
                "missing": sorted(expected - actual) if expected is not None else None,
                "unexpected": sorted(actual - expected) if expected is not None else None,
                "basis": "UNQUALIFIED_ENTRIES" if expected is not None else "UNKNOWN_RUNNERS",
                "source_updated_at": None,
            }
        result[race_id] = {
            "markets": markets,
            "absent_markets": sorted(
                {
                    "win",
                    "place",
                    "quinella",
                    "exacta",
                    "wide",
                    "trio",
                    "trifecta",
                    "bracket_quinella",
                    "bracket_exacta",
                }
                - markets.keys()
            ),
            "paper_eligible": False,
        }
    return result


class RealData:
    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)

    def inspect(self, raw, filename, dataset_kind, encoding):
        if dataset_kind not in {"DAILY_SNAPSHOT", "FINAL_ONLY"}:
            raise ValueError("DATASET_KIND")
        meta = filename_metadata(filename)
        if meta["filename_kind"] == "FINAL_ONLY" and dataset_kind != "FINAL_ONLY":
            raise ValueError("MONTHLY_IS_FINAL_ONLY")
        # Archive even a schema-incompatible ZIP before parsing; do not lose evidence.
        if len(raw) > MAX_COMPRESSED:
            raise ValueError("INPUT_LIMIT")
        digest = self.store.body(raw, "raw")
        saved = stamp(self.store.clock())
        recipe = {
            "filename": filename,
            "dataset_kind": dataset_kind,
            "encoding": encoding,
            "odds_version": VERSION,
            "race_version": RACE_VERSION,
        }
        inspection_id = identity([digest, recipe])
        old = self.store.db.execute(
            "SELECT report_hash FROM asset_inspections WHERE id=?", (inspection_id,)
        ).fetchone()
        if old:
            return json.loads(self.store.read_body(old[0], "reports"))
        started = time.perf_counter()
        report = {
            "id": inspection_id,
            "raw_hash": digest,
            "raw_bytes": len(raw),
            "raw_saved_at": saved,
            "recipe": recipe,
            **meta,
            "source_updated_at": None,
            "collector_received_at": None,
            "availability_basis": "LOCAL_INSPECTION_ONLY",
            "live_qualified": False,
            "paper_eligible": False,
        }
        try:
            files = unzip(raw)
            report["members"] = [{"name": k, "bytes": len(v), "sha256": sha(v)} for k, v in files.items()]
            if any(name.endswith("_odds.csv") for name in files):
                if len(files) != 1:
                    raise ValueError("UNEXPECTED_ZIP_MEMBER")
                odds = parse_odds(raw, {}, encoding)
                report.update(content_type="odds", content=odds, coverage=completeness(odds))
            else:
                report.update(content_type="race", content=parse_race_bundle(raw, encoding))
            report["status"] = "PARSED_UNQUALIFIED"
        except (ValueError, UnicodeError, KeyError, OverflowError, csv.Error) as exc:
            # Class only: invalid numeric cells and decoder errors can include real source values.
            report.update(status="QUARANTINED", error_class=type(exc).__name__)
        report.update(
            inspected_at=stamp(self.store.clock()), duration_ms=(time.perf_counter() - started) * 1000
        )
        report_hash = self.store.body(canonical(report), "reports")
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO asset_inspections VALUES(?,?,?,?,?)",
                (inspection_id, digest, saved, report["inspected_at"], report_hash),
            )
        return report

    def import_capture(self, manifest, raw, encoding):
        """Import an exported, completed Worker manifest. Keep the receipt verbatim.

        A manifest is provenance supplied from private storage, not cryptographic
        proof of origin. Do not take receipts from public/untrusted callers.
        """
        now = stamp(self.store.clock())
        validate_manifest(manifest, raw, now)
        if encoding not in {"utf-8-sig", "cp932"}:
            raise ValueError("ENCODING_UNQUALIFIED")
        event_id = manifest["event_id"]
        receipt_hash = identity(manifest)
        previous = self.store.db.execute(
            "SELECT * FROM capture_imports WHERE event_id=?", (event_id,)
        ).fetchone()
        if previous and (previous["receipt_hash"] != receipt_hash or previous["encoding"] != encoding):
            raise ValueError("CAPTURE_IMPORT_CONFLICT")
        existing_attempt = self.store.db.execute(
            "SELECT event FROM attempts WHERE id=?", (event_id,)
        ).fetchone()
        if existing_attempt and (
            not previous or json.loads(existing_attempt[0]).get("receipt_hash") != receipt_hash
        ):
            raise ValueError("CAPTURE_IMPORT_CONFLICT")
        self.store.body(raw, "raw")
        self.store.body(canonical(manifest), "receipts")
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO capture_imports VALUES(?,?,?,?,?)",
                (event_id, receipt_hash, sha(raw), now, encoding),
            )
        saved = self.store.db.execute(
            "SELECT imported_at FROM capture_imports WHERE event_id=?", (event_id,)
        ).fetchone()[0]
        event = {
            "attempt_id": event_id,
            "scheduled_capture_at": manifest["scheduled_capture_at"],
            "fetch_started_at": manifest["fetch_started_at"],
            "collector_received_at": manifest["collector_received_at"],
            "ingest_received_at": saved,
            "status": manifest["http_status"],
            "dataset_kind": "DAILY_SNAPSHOT",
            "availability_basis": "EXPORTED_COLLECTOR_RECEIPT",
            "source": "nar-daily-odds",
            "receipt_hash": receipt_hash,
            "collector_raw_saved_at": manifest["raw_saved_at"],
            "headers_received_at": manifest["headers_received_at"],
            "encoding": encoding,
            "file_timestamp": manifest["file_timestamp"],
            "file_name": manifest["file_name"],
            "source_updated_at": None,
            "source_published_at": None,
            "race_states": {},
            "etag": manifest["etag"],
            "retry_of": None,
        }
        if existing_attempt:
            old_event = json.loads(existing_attempt[0])
            expected = {**event, "raw_hash": sha(raw)}
            for field in (
                "scheduled_capture_at",
                "fetch_started_at",
                "collector_received_at",
                "ingest_received_at",
            ):
                expected[field] = stamp(expected[field])
            allowed = {"validator_attempt", "validator"} if manifest["http_status"] == 304 else set()
            if {k: v for k, v in old_event.items() if k not in allowed} != expected:
                raise ValueError("CAPTURE_IMPORT_CONFLICT")
            # Reuse the validated first binding even if another earlier 200 arrived later.
            return self.store.ingest(old_event, raw, f"{VERSION}:{encoding}")
        if manifest["http_status"] == 304:
            prior = []
            for row in self.store.db.execute("SELECT event FROM attempts"):
                candidate = json.loads(row[0])
                if (
                    candidate.get("source") == "nar-daily-odds"
                    and candidate["status"] == 200
                    and candidate.get("etag") == manifest["validator_sent"]
                    and candidate.get("raw_hash") == manifest["validator_raw_sha256"]
                    and candidate["collector_received_at"] < stamp(manifest["fetch_started_at"])
                ):
                    prior.append(candidate)
            if not prior:
                raise ValueError("IMPORT_VALIDATOR_200_FIRST")
            bound = max(prior, key=lambda x: x["collector_received_at"])
            event.update(validator_attempt=bound["attempt_id"], validator=manifest["validator_sent"])
        return self.store.ingest(event, raw, f"{VERSION}:{encoding}")


def validate_manifest(m, raw, now):
    if not isinstance(m, dict) or set(m) != MANIFEST_FIELDS:
        raise ValueError("MANIFEST_SCHEMA")
    if not re.fullmatch(r"nar-daily-odds:\d+", str(m["event_id"])):
        raise ValueError("MANIFEST_SOURCE")
    if m["http_status"] not in (200, 304) or type(m["raw_bytes"]) is not int:
        raise ValueError("MANIFEST_STATUS")
    if len(raw) > MAX_COMPRESSED or len(raw) != m["raw_bytes"] or sha(raw) != m["raw_sha256"]:
        raise ValueError("MANIFEST_BODY_HASH")
    if not raw.startswith(b"PK"):
        raise ValueError("MANIFEST_BODY_TYPE")
    for field in (
        "scheduled_capture_at",
        "fetch_started_at",
        "headers_received_at",
        "collector_received_at",
        "raw_saved_at",
    ):
        if not isinstance(m[field], str):
            raise ValueError("MANIFEST_TIME")
    times = [
        stamp(m[k])
        for k in ("fetch_started_at", "headers_received_at", "collector_received_at", "raw_saved_at")
    ]
    if times != sorted(times) or times[-1] > now:
        raise ValueError("MANIFEST_TIME")
    scheduled = instant(m["scheduled_capture_at"])
    if (
        int(scheduled.timestamp() * 1000) != int(m["event_id"].split(":")[1])
        or stamp(m["scheduled_capture_at"]) > times[0]
    ):
        raise ValueError("MANIFEST_SLOT")
    if type(m["duration_ms"]) not in (int, float) or not 0 <= m["duration_ms"] < 86400000:
        raise ValueError("MANIFEST_DURATION")
    for field in ("file_name", "file_timestamp", "etag", "validator_sent", "validator_raw_sha256"):
        if m[field] is not None and (not isinstance(m[field], str) or len(m[field]) > 1024):
            raise ValueError("MANIFEST_FIELD")
    meta = filename_metadata(m["file_name"])
    if meta["filename_kind"] == "FINAL_ONLY":
        raise ValueError("MONTHLY_IS_FINAL_ONLY")
    if m["file_timestamp"] is not None:
        if meta["file_timestamp"] is None or stamp(m["file_timestamp"]) != stamp(meta["file_timestamp"]):
            raise ValueError("FILENAME_TIME_MISMATCH")
    if m["http_status"] == 304 and (
        not m["validator_sent"]
        or m["validator_raw_sha256"] != m["raw_sha256"]
        or m["etag"] != m["validator_sent"]
    ):
        raise ValueError("UNBOUND_304")
