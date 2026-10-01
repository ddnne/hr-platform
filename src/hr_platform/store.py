"""Local persistent reference store: append-only observations and parse revisions.

Bodies precede index writes. Orphans are harmless and reused after an index failure.
SQLite transactions make each index publication atomic; no cross-store atomicity assumed.
"""

import csv
import json
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .common import canonical, identity, sha, stamp, seconds, utcnow, instant
from .parser import parse_odds, VERSION
from .history import asof_view

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS plans(slot TEXT PRIMARY KEY, required INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, event TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY REFERENCES attempts(id), raw_hash TEXT NOT NULL,
 raw_saved_at TEXT NOT NULL, received_at TEXT NOT NULL, basis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parses(id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(id),
 version TEXT NOT NULL, parsed_at TEXT NOT NULL, available_at TEXT, status TEXT NOT NULL, error TEXT,
 duration_ms REAL NOT NULL, UNIQUE(observation_id,version));
CREATE TABLE IF NOT EXISTS snapshots(parse_id TEXT NOT NULL REFERENCES parses(id), race_id TEXT NOT NULL,
 market TEXT NOT NULL, body_hash TEXT NOT NULL, state_hash TEXT NOT NULL, PRIMARY KEY(parse_id,race_id,market));
CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(race_id,market);
CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, config_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, experiment TEXT NOT NULL, model TEXT NOT NULL,
 race_id TEXT NOT NULL, day TEXT NOT NULL, stake INTEGER NOT NULL, body TEXT NOT NULL,
 UNIQUE(experiment,model,race_id));
CREATE TABLE IF NOT EXISTS settlements(id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
 revision TEXT NOT NULL, available_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(decision_id,revision));
"""


class Store:
    def __init__(self, root, clock=utcnow):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in [self.root, *self.root.parents]):
            raise ValueError("STORE_SYMLINK")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in (
            "raw",
            "normalized",
            "receipts",
            "reports",
            "index.sqlite",
            "index.sqlite-wal",
            "index.sqlite-shm",
            "index.sqlite-journal",
        ):
            self.safe_path(name)
        self.clock = clock
        self.db = sqlite3.connect(self.root / "index.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def safe_path(self, *parts):
        path = self.root
        for part in parts:
            if not part or Path(part).name != part or part in {".", ".."}:
                raise ValueError("STORE_PATH")
            path = path / part
            if path.is_symlink():
                raise ValueError("STORE_SYMLINK")
            if path.is_file() and path.stat().st_nlink != 1:
                raise ValueError("STORE_HARDLINK")
        return path

    def close(self):
        self.db.close()

    def body(self, data, kind):
        digest = sha(data)
        path = self.safe_path(kind, digest)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            if sha(path.read_bytes()) != digest:
                raise ValueError("BODY_CORRUPT")
        else:
            temp = path.with_name(f".{digest}.{uuid.uuid4().hex}")
            with temp.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        return digest

    def read_body(self, digest, kind):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("INVALID_HASH")
        raw = self.safe_path(kind, digest).read_bytes()
        if sha(raw) != digest:
            raise ValueError("BODY_CORRUPT")
        return raw

    def plan(self, slots):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO plans VALUES(?,1)", [(stamp(t),) for t in slots])

    def ingest(self, event, raw=None, version=VERSION, parser=parse_odds):
        event = dict(event)
        for key in (
            "scheduled_capture_at",
            "fetch_started_at",
            "ingest_received_at",
        ):
            event[key] = stamp(event[key])
        received = event.get("collector_received_at")
        event["collector_received_at"] = stamp(received) if received is not None else None
        accepted = event.get("response_accepted", True)
        if (event["fetch_started_at"] > event["ingest_received_at"]
            or received is not None and not event["fetch_started_at"] <= event["collector_received_at"] <= event["ingest_received_at"]
            or received is None and accepted and (event["status"] == 304 or event["status"] == 200 and raw is not None)):
            raise ValueError("CLOCK_ORDER")
        if event["dataset_kind"] not in {"SYNTHETIC", "DAILY_SNAPSHOT", "FINAL_ONLY"}:
            raise ValueError("DATASET_KIND")
        if event.get("source_updated_at") is not None:
            raise ValueError("SOURCE_TIME_MUST_BE_MARKET_SPECIFIC")
        attempt = event["attempt_id"]
        digest = sha(raw) if raw is not None else None
        # 304 is valid only against exactly the validator sent and its saved body.
        if event["status"] == 304 and accepted:
            prior = self.db.execute(
                "SELECT event FROM attempts WHERE id=?", (event.get("validator_attempt"),)
            ).fetchone()
            if not prior:
                raise ValueError("UNBOUND_304")
            prior = json.loads(prior[0])
            if (
                not event.get("validator")
                or event["validator"] != prior.get("etag")
                or prior["status"] != 200
            ):
                raise ValueError("UNBOUND_304")
            digest = prior.get("raw_hash")
            raw = self.read_body(digest, "raw")
        event["raw_hash"] = digest
        fingerprint = identity(event)
        old = self.db.execute("SELECT fingerprint FROM attempts WHERE id=?", (attempt,)).fetchone()
        if old and old[0] != fingerprint:
            raise ValueError("EVENT_ID_CONFLICT")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO attempts VALUES(?,?,?)",
                (attempt, canonical(event).decode(), fingerprint),
            )
        if not accepted or event["status"] not in (200, 304) or raw is None:
            return None
        obs = self.db.execute("SELECT * FROM observations WHERE id=?", (attempt,)).fetchone()
        if not obs:
            self.body(raw, "raw")
            saved = stamp(self.clock())
            if saved < event["ingest_received_at"]:
                raise ValueError("CLOCK_ORDER")
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                    (
                        attempt,
                        digest,
                        saved,
                        event["collector_received_at"],
                        "validator_304" if event["status"] == 304 else "body_200",
                    ),
                )
        return self.reparse(attempt, version, parser)

    def reparse(self, observation_id, version, parser=parse_odds):
        parse_id = identity([observation_id, version])
        old = self.db.execute(
            "SELECT status,available_at,parsed_at FROM parses WHERE id=?", (parse_id,)
        ).fetchone()
        if old:
            if old["status"] == "OK" and old["available_at"] is None:
                available = stamp(self.clock())
                if available < old["parsed_at"]:
                    raise ValueError("CLOCK_ORDER")
                with self.db:
                    self.db.execute(
                        "UPDATE parses SET available_at=? WHERE id=? AND available_at IS NULL",
                        (available, parse_id),
                    )
            return parse_id  # successful or failed delivery does not rewrite time; repair needs new version
        obs = self.db.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
        if not obs:
            raise ValueError("OBSERVATION_MISSING")
        event = json.loads(
            self.db.execute("SELECT event FROM attempts WHERE id=?", (observation_id,)).fetchone()[0]
        )
        start = time.perf_counter()
        try:
            races = parser(
                self.read_body(obs["raw_hash"], "raw"),
                event["race_states"],
                event.get("encoding", "utf-8-sig"),
            )
            rows = []
            for race_id, race in races.items():
                for market, quotes in race["markets"].items():
                    content = {
                        "normalization_version": version,
                        "state": race["state"],
                        "market": market,
                        **quotes,
                    }
                    content_hash = self.body(canonical(content), "normalized")
                    rows.append((parse_id, race_id, market, content_hash, identity(race["state"])))
            status, error = "OK", None
        except (ValueError, UnicodeError, KeyError, csv.Error) as exc:
            status, error, rows = "ERROR", type(exc).__name__, []
        parsed = stamp(self.clock())
        if parsed < obs["raw_saved_at"]:
            raise ValueError("CLOCK_ORDER")
        with self.db:
            self.db.execute(
                "INSERT INTO parses VALUES(?,?,?,?,?,?,?,?)",
                (
                    parse_id,
                    observation_id,
                    version,
                    parsed,
                    None,
                    status,
                    error,
                    (time.perf_counter() - start) * 1000,
                ),
            )
            self.db.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?)", rows)
        if status == "OK":
            available = stamp(self.clock())
            if available < parsed:
                raise ValueError("CLOCK_ORDER")
            with self.db:
                self.db.execute(
                    "UPDATE parses SET available_at=? WHERE id=? AND available_at IS NULL",
                    (available, parse_id),
                )
        return parse_id

    def history(self, race_id, market, until=None, *, current_only=False, since=None, dataset_kind=None):
        if dataset_kind not in {None, 'SYNTHETIC', 'DAILY_SNAPSHOT', 'FINAL_ONLY'}:
            raise ValueError('DATASET_KIND')
        cutoff = stamp(until) if until else "9999-12-31T23:59:59.999999+00:00"
        start = stamp(since) if since is not None else None
        if start is not None and start > cutoff:
            raise ValueError("HISTORY_WINDOW_REVERSED")
        rows = self.db.execute(
            """SELECT s.*, p.version,p.parsed_at,p.available_at,o.id observation_id,
          o.raw_hash,o.raw_saved_at,o.received_at,o.basis,a.event FROM snapshots s
          JOIN parses p ON p.id=s.parse_id JOIN observations o ON o.id=p.observation_id
          JOIN attempts a ON a.id=o.id WHERE s.race_id=? AND s.market=? AND p.available_at<=?
          AND (? IS NULL OR json_extract(a.event,'$.dataset_kind')=?)
          AND (? IS NULL OR o.received_at>=?)
          AND (NOT ? OR NOT EXISTS (SELECT 1 FROM parses newer
            WHERE newer.observation_id=p.observation_id AND newer.available_at<=?
            AND (newer.available_at,newer.id)>(p.available_at,p.id)))
          ORDER BY o.received_at,p.available_at,p.id""",
            (race_id, market, cutoff, dataset_kind, dataset_kind, start, start, current_only, cutoff),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["event"] = json.loads(item["event"])
            item["content"] = json.loads(self.read_body(item["body_hash"], "normalized"))
            result.append(item)
        return result

    def gaps(self, asof):
        cutoff = stamp(asof)
        result = []
        for (slot,) in self.db.execute(
            "SELECT slot FROM plans WHERE required=1 AND slot<=? ORDER BY slot", (cutoff,)
        ):
            attempts = [json.loads(r[0]) for r in self.db.execute("SELECT event FROM attempts")]
            planned = [
                a for a in attempts if a["scheduled_capture_at"] == slot and a["ingest_received_at"] <= cutoff
            ]
            # A retry received later is not a capture at the planned slot.
            deadline = min(cutoff, stamp((instant(slot) + timedelta(seconds=120)).isoformat()))
            usable = [
                a
                for a in planned
                if not a.get("retry_of")
                and 0 <= seconds(a["fetch_started_at"], slot) < 120
                and a["collector_received_at"] is not None
                and a["collector_received_at"] <= deadline
                and self.db.execute(
                    "SELECT 1 FROM parses WHERE observation_id=? AND available_at<=?",
                    (a["attempt_id"], deadline),
                ).fetchone()
            ]
            if not usable:
                result.append(
                    {"slot": slot, "reason": "NOT_EXECUTED" if not planned else "FAILED_OR_NOT_AVAILABLE"}
                )
        return result

    def asof(self, race_id, markets, at, max_age=300):
        histories = {h: self.history(race_id, h, at, current_only=True) for h in markets}
        return asof_view(histories, markets, at, max_age, self.gaps(at))

    def latest(self, race_id, market):
        rows = self.history(race_id, market, current_only=True)
        return rows[-1] if rows else None

    def metrics(self):
        return {
            "attempts": self.db.execute("SELECT count(*) FROM attempts").fetchone()[0],
            "observations": self.db.execute("SELECT count(*) FROM observations").fetchone()[0],
            "parse_errors": self.db.execute("SELECT count(*) FROM parses WHERE status='ERROR'").fetchone()[0],
            "raw_objects": len(list((self.root / "raw").glob("*"))),
            "raw_bytes": sum(p.stat().st_size for p in (self.root / "raw").glob("*")),
            "normalized_objects": len(list((self.root / "normalized").glob("*"))),
            "parse_ms": self.db.execute("SELECT coalesce(sum(duration_ms),0) FROM parses").fetchone()[0],
        }
