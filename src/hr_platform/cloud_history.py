"""R2/D1 odds history using the existing CSV parser; no provider fetch or Paper.

Only already-published raw observations can be parsed. Body hashes deduplicate
content, while each observation and parser version keeps its own availability.
"""

import asyncio
import csv
import json
import math
import re
from .common import canonical, identity, sha, stamp, utcnow
from .history import asof_view
from .parser import (iter_odds_races, iter_monthly_odds_races, VERSION as PARSER_VERSION,
                     MONTHLY_VERSION, MAX_COMPRESSED, MAX_EXPANDED)

VERSION = f"cloud-odds-v3:{PARSER_VERSION}"
FINAL_VERSION = f"cloud-monthly-v1:{MONTHLY_VERSION}"
HISTORY_PAGE_BYTES = 1024 * 1024
PUBLICATION_CLOCK = "strftime('%Y-%m-%dT%H:%M:%f000+00:00','now')"


def native(value):
    return value.to_py() if hasattr(value, "to_py") else value


def storage_limits(policy):
    # Missing policy preserves the serial behavior for existing local callers.
    if policy is None:
        return 1, MAX_EXPANDED
    if (not isinstance(policy, dict) or set(policy) != {
        'version', 'normalization_concurrency', 'normalization_batch_bytes', 'worker_cpu_ms'
    } or policy['version'] != 'cloud-storage-v1'):
        raise ValueError('STORAGE_POLICY')
    concurrency, size = policy['normalization_concurrency'], policy['normalization_batch_bytes']
    if (type(concurrency) is not int or not 1 <= concurrency <= 4
            or type(size) is not int or not 0 < size <= MAX_EXPANDED
            or type(policy['worker_cpu_ms']) is not int or policy['worker_cpu_ms'] <= 0):
        raise ValueError('STORAGE_POLICY')
    return concurrency, size


class CloudHistory:
    body_prefix = 'odds-normalized'
    parse_table = 'odds_parses'
    races_table = 'odds_races'

    def __init__(self, bucket, database, clock=utcnow, *, storage_policy=None):
        self.bucket, self.db, self.clock = bucket, database, clock
        self.concurrency, self.batch_bytes = storage_limits(storage_policy)

    async def first(self, sql, *args):
        return native(await self.db.prepare(sql).bind(*args).first())

    async def run(self, sql, *args):
        return native(await self.db.prepare(sql).bind(*args).run())

    async def all(self, sql, *args):
        return native(await self.db.prepare(sql).bind(*args).all())['results']

    async def body(self, key, limit):
        return await self.object_body(await self.bucket.get(key), limit)

    async def object_body(self, obj, limit):
        if obj is None or obj.size > limit:
            raise ValueError("BODY_MISSING_OR_LIMIT")
        data = bytes(native(await obj.arrayBuffer()))
        if len(data) > limit:
            raise ValueError("BODY_LIMIT")
        return data

    async def normalize(self, observation_id, encoding="utf-8-sig"):
        if not isinstance(observation_id, str) or not re.fullmatch(
            r"nar-(?:daily-(?:odds|monthly):\d+|mac-import:[0-9a-f]{64})", observation_id,
        ):
            raise ValueError("OBSERVATION_ID")
        if encoding not in {"utf-8-sig", "cp932"}:
            raise ValueError("ENCODING_UNQUALIFIED")
        observation = await self.first("SELECT * FROM raw_observations WHERE observation_id=?", observation_id)
        if not observation or observation["dataset_kind"] not in {"DAILY_SNAPSHOT", "FINAL_ONLY", "SYNTHETIC"}:
            raise ValueError("OBSERVATION_MISSING_OR_KIND")
        monthly = observation_id.startswith('nar-daily-monthly:')
        if monthly and observation['dataset_kind'] != 'FINAL_ONLY':
            raise ValueError('MONTHLY_IS_FINAL_ONLY')
        version = FINAL_VERSION if monthly else VERSION
        parse_id = identity([observation_id, version, encoding])
        old = await self.first("SELECT * FROM odds_parses WHERE parse_id=?", parse_id)
        if old and (old["available_at"] or old["status"] == "ERROR"):
            return old
        if stamp(observation["raw_saved_at"]) > stamp(self.clock()):
            raise ValueError("CLOCK_ORDER")
        digest = observation["raw_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("RAW_HASH")
        raw = await self.body(f"raw/{digest}", MAX_COMPRESSED)
        if sha(raw) != digest:
            raise ValueError("BODY_CORRUPT")
        races, total, pending, pending_bytes = {}, 0, [], 0
        try:
            if monthly:
                capture = await self.first('SELECT file_name FROM captures WHERE event_id=?', observation_id)
                filename = capture['file_name'] if capture else None
                match = re.fullmatch(r'(\d{6})_\d{10}_odds\.zip', filename or '')
                if not match:
                    raise ValueError('MONTHLY_FILENAME')
                iterator = iter_monthly_odds_races(raw, {}, match[1], encoding)
            else:
                iterator = iter_odds_races(raw, {}, encoding)
            for race_id, race in iterator:
                data = canonical(race)
                total += len(data)
                # Monthly parts are stored by race, so their aggregate JSON
                # need not fit a daily archive's bound. Every readable object,
                # the source ZIP and each in-flight batch remain bounded.
                if len(data) > MAX_EXPANDED or (not monthly and total > MAX_EXPANDED):
                    raise ValueError("NORMALIZED_LIMIT")
                if pending and pending_bytes + len(data) > self.batch_bytes:
                    races.update(await self.save_race_batch(pending))
                    pending, pending_bytes = [], 0
                pending.append((race_id, list(race['markets']), data))
                pending_bytes += len(data)
                # One race larger than the budget is handled on its own, as in v2.
                if len(pending) == self.concurrency or pending_bytes >= self.batch_bytes:
                    races.update(await self.save_race_batch(pending))
                    pending, pending_bytes = [], 0
            if pending:
                races.update(await self.save_race_batch(pending))
            index = canonical({'format': 'odds-races-v2', 'races': races})
            if len(index) > MAX_EXPANDED:
                raise ValueError('NORMALIZED_LIMIT')
            digest = await self.save_body(index)
        except (ValueError, UnicodeError, KeyError, csv.Error) as exc:
            await self.run("INSERT OR IGNORE INTO odds_parses VALUES(?,?,?,?,'ERROR',?,NULL,NULL,?)",
                           parse_id, observation_id, version, encoding, stamp(self.clock()), type(exc).__name__)
            return await self.first("SELECT * FROM odds_parses WHERE parse_id=?", parse_id)
        if old and old["body_hash"] != digest:
            raise ValueError("PARSE_VERSION_CONFLICT")
        # Sample after the storage I/O, so a frozen CPU clock is not reported as
        # the completion of parsing. No duration here claims billed CPU time.
        parsed_at = stamp(self.clock())
        await self.run("INSERT OR IGNORE INTO odds_parses VALUES(?,?,?,?,'WRITING',?,NULL,?,NULL)",
                       parse_id, observation_id, version, encoding, parsed_at, digest)
        stored = await self.first("SELECT * FROM odds_parses WHERE parse_id=?", parse_id)
        if stored["body_hash"] != digest:
            raise ValueError("PARSE_VERSION_CONFLICT")
        await self.run("""INSERT OR IGNORE INTO odds_races
            SELECT ?,json_extract(value,'$.race_id'),json_extract(value,'$.markets') FROM json_each(?)""",
                       parse_id, json.dumps([{'race_id': r, 'markets': v['markets']} for r, v in races.items()]))
        # D1 stamps the publication statement when it executes, not when the
        # client sends it. Network/queue delay cannot backdate availability.
        await self.run(f"""UPDATE odds_parses SET status='OK',available_at={PUBLICATION_CLOCK}
            WHERE parse_id=? AND available_at IS NULL AND parsed_at<={PUBLICATION_CLOCK}""", parse_id)
        published = await self.first("SELECT * FROM odds_parses WHERE parse_id=?", parse_id)
        if published['available_at'] is None:
            raise ValueError('CLOCK_ORDER')
        return published

    async def save_race_batch(self, pending):
        # Wait for every storage operation even on failure. Nothing is published
        # and no task is left writing after normalize returns an error.
        results = await asyncio.gather(*(self.save_body(data) for _, _, data in pending),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return {race_id: {'body_hash': digest, 'markets': markets}
                for (race_id, markets, _), digest in zip(pending, results)}

    async def save_body(self, data):
        digest = sha(data)
        key = f"{self.body_prefix}/{digest}"
        existing = await self.bucket.get(key)
        if existing is not None:
            if sha(await self.object_body(existing, MAX_EXPANDED)) != digest:
                raise ValueError("BODY_CORRUPT")
        else:
            await self.bucket.put(key, data.decode())
        return digest

    async def read_body(self, digest):
        data = await self.body(f"{self.body_prefix}/{digest}", MAX_EXPANDED)
        if sha(data) != digest:
            raise ValueError('BODY_CORRUPT')
        return json.loads(data)

    async def race_body(self, row, race_id):
        data = await self.read_body(row['body_hash'])
        if data.get('format') in {'odds-races-v2', 'race-files-v1'}:
            return await self.read_body(data['races'][race_id]['body_hash'])
        return data[race_id]  # Preserve history from the original whole-archive format.

    def cutoff(self, at):
        cutoff = stamp(at)
        if cutoff > stamp(self.clock()):
            raise ValueError('ASOF_IN_FUTURE')
        return cutoff

    async def history(self, race_id, at, cursor=None, limit=50, since=None):
        cutoff = self.cutoff(at)
        lower = stamp(since) if since is not None else ''
        if lower > cutoff:
            raise ValueError('HISTORY_WINDOW')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('PAGE_LIMIT')
        cursor_at, cursor_id = '', ''
        if cursor is not None:
            if not isinstance(cursor, str) or not re.fullmatch(r'[0-9a-f]{64}', cursor):
                raise ValueError('CURSOR')
            previous = await self.first(f"""SELECT p.available_at FROM {self.parse_table} p JOIN {self.races_table} r USING(parse_id)
                WHERE p.parse_id=? AND r.race_id=? AND p.status='OK' AND p.available_at<=?""", cursor, race_id, cutoff)
            if not previous:
                raise ValueError('CURSOR')
            cursor_at, cursor_id = previous['available_at'], cursor
        rows = native(await self.db.prepare(f"""SELECT p.*,o.raw_sha256,o.received_at,o.raw_saved_at,
            o.evidence,o.dataset_kind FROM {self.races_table} r JOIN {self.parse_table} p USING(parse_id)
            JOIN raw_observations o USING(observation_id)
            WHERE r.race_id=? AND p.status='OK' AND p.available_at<=? AND o.received_at>=?
            AND (p.available_at,p.parse_id)>(?,?)
            ORDER BY p.available_at,p.parse_id LIMIT ?""").bind(race_id, cutoff, lower, cursor_at, cursor_id, limit + 1).all())['results']
        result, size = [], 0
        for row in rows[:limit]:
            item = {**row, 'race_id': race_id, 'content': await self.race_body(row, race_id)}
            item_size = len(canonical(item))
            if item_size > HISTORY_PAGE_BYTES:
                raise ValueError('HISTORY_ITEM_LIMIT')
            if size + item_size > HISTORY_PAGE_BYTES:
                break
            result.append(item)
            size += item_size
        return {'history': result, 'asof_at': cutoff, 'paper_eligible': False,
                'next_cursor': result[-1]['parse_id'] if len(rows) > len(result) else None,
                'availability_clock': 'D1_PUBLICATION_STATEMENT_UTC'}

    async def asof(self, race_id, markets, at, max_age=300):
        cutoff = self.cutoff(at)
        if type(max_age) not in {int, float} or not math.isfinite(max_age) or max_age < 0:
            raise ValueError('MAX_AGE')
        records = await self._market_records(race_id, markets, cutoff, final_only=False)
        return {**asof_view({m: [r] for m, r in records.items()}, markets, at, max_age),
                'gap_coverage': 'UNQUALIFIED_CAPTURE_PLAN', 'paper_eligible': False,
                'availability_clock': 'D1_PUBLICATION_STATEMENT_UTC'}

    async def final_prices(self, race_id, markets, at):
        """Post-race prices only; never an eligible pre-race input view."""
        cutoff = self.cutoff(at)
        records = await self._market_records(race_id, markets, cutoff, final_only=True)
        return {'race_id': race_id, 'asof_at': cutoff, 'markets': records,
                'missing_markets': [m for m in markets if m not in records],
                'purpose': 'POST_RACE_FINAL_PRICE_EVALUATION', 'paper_eligible': False,
                'availability_clock': 'D1_PUBLICATION_STATEMENT_UTC'}

    async def _market_records(self, race_id, markets, cutoff, *, final_only):
        """Resolve both price uses through one query and body reader."""
        from .parser import MARKETS

        if (not isinstance(markets, list) or not markets or any(h not in MARKETS.values() for h in markets)
                or len(set(markets)) != len(markets)):
            raise ValueError('MARKETS')
        records, bodies = {}, {}
        for market in markets:
            # Resolve the latest successful parse of each observation BEFORE
            # filtering by race/market. A removed market/race cannot resurrect.
            row = await self.first(f"""SELECT p.*,o.raw_sha256,o.received_at,o.raw_saved_at,o.dataset_kind
                FROM odds_parses p JOIN raw_observations o USING(observation_id)
                JOIN odds_races r USING(parse_id)
                WHERE p.status='OK' AND p.available_at<=? AND o.dataset_kind{'=' if final_only else '!='}'FINAL_ONLY'
                AND NOT EXISTS (SELECT 1 FROM odds_parses newer
                  WHERE newer.observation_id=p.observation_id AND newer.status='OK' AND newer.available_at<=?
                  AND (newer.available_at,newer.parse_id)>(p.available_at,p.parse_id))
                AND r.race_id=? AND EXISTS (SELECT 1 FROM json_each(r.markets) WHERE value=?)
                ORDER BY o.received_at DESC,p.available_at DESC,p.parse_id DESC LIMIT 1""",
                cutoff, cutoff, race_id, market)
            if row is None:
                continue
            if row['body_hash'] not in bodies:
                bodies[row['body_hash']] = await self.race_body(row, race_id)
            race = bodies[row['body_hash']]
            records[market] = {
                **{k: row[k] for k in ('parse_id', 'observation_id', 'available_at', 'received_at', 'raw_saved_at')},
                'raw_hash': row['raw_sha256'], 'body_hash': row['body_hash'],
                'state_hash': identity(race['state']), 'event': {'dataset_kind': row['dataset_kind']},
                'content': {'state': race['state'], 'market': market, **race['markets'][market]},
            }
        return records
