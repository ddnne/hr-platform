"""Publish existing official state/payout adapters over saved Cloudflare captures.

No fetch, inferred finality or Paper write. Raw receipt, parse and publication
times remain distinct; an unqualified newer page never falls back to an old one.
"""
import csv
import json
import re
from .cloud_history import CloudHistory, PUBLICATION_CLOCK, HISTORY_PAGE_BYTES
from .common import canonical, identity, sha, stamp, seconds
from .race_state import StateEvidence, MAX_BYTES, VERSION as STATE_VERSION, parse_state_page, validate_receipt
from .official_payout import PayoutEvidence, VERSION as PAYOUT_VERSION, parse_payout_page

VERSIONS = {'state': f'cloud-page-v1:{STATE_VERSION}', 'payout': f'cloud-page-v1:{PAYOUT_VERSION}'}
ADAPTERS = {'state': StateEvidence, 'payout': PayoutEvidence}
PARSERS = {'state': parse_state_page, 'payout': parse_payout_page}


class CloudPages(CloudHistory):
    body_prefix = 'page-normalized'

    async def normalize(self, observation_id):
        if not isinstance(observation_id, str) or not re.fullmatch(r'nar-daily-(state|payout):\d+', observation_id):
            raise ValueError('OBSERVATION_ID')
        plan = await self.first('SELECT * FROM page_capture_plans WHERE event_id=?', observation_id)
        obs = await self.first('SELECT * FROM raw_observations WHERE observation_id=?', observation_id)
        if not plan or plan['kind'] not in VERSIONS or not obs or obs['dataset_kind'] != f"NAR_PAGE_{plan['kind'].upper()}":
            raise ValueError('PAGE_OBSERVATION_OR_PLAN')
        kind, race_id = plan['kind'], plan['race_id']
        parse_id = identity([observation_id, VERSIONS[kind]])
        existing = await self.first('SELECT * FROM page_parses WHERE parse_id=?', parse_id)
        if existing:
            await self.read_body(existing['body_hash'])
            await self.run(f'''UPDATE page_parses SET available_at={PUBLICATION_CLOCK}
                WHERE parse_id=? AND available_at IS NULL AND parsed_at<={PUBLICATION_CLOCK}''', parse_id)
            return await self.first('SELECT * FROM page_parses WHERE parse_id=?', parse_id)
        manifest = json.loads(await self.body(f'manifests/{observation_id}.json', MAX_BYTES))
        if (manifest['event_id'] != observation_id or manifest.get('url') != plan['url']
                or manifest.get('race_id') != race_id or manifest['raw_sha256'] != obs['raw_sha256']
                or stamp(manifest['collector_received_at']) != stamp(obs['received_at'])
                or stamp(manifest['raw_saved_at']) != stamp(obs['raw_saved_at'])
                or stamp(plan['registered_at']) > stamp(manifest['fetch_started_at'])):
            raise ValueError('PAGE_RECEIPT_CONFLICT')
        if not re.fullmatch(r'[0-9a-f]{64}', obs['raw_sha256']):
            raise ValueError('RAW_HASH')
        raw = await self.body(f"raw/{obs['raw_sha256']}", MAX_BYTES)
        receipt = {**{key: manifest[key] for key in (
            'url', 'fetch_started_at', 'headers_received_at', 'collector_received_at', 'raw_saved_at')},
            'sha256': manifest['raw_sha256'], 'status': manifest['http_status'], 'bytes': manifest['raw_bytes']}
        # Reuse the exact source/time and venue validations of the local adapter.
        adapter = ADAPTERS[kind]
        validate_receipt(receipt, raw, race_id, stamp(self.clock()), adapter.receipt_path)
        report = {'id': parse_id, 'observation_id': observation_id, 'version': VERSIONS[kind],
                  'raw_hash': sha(raw), 'receipt_hash': identity(receipt), 'race_id': race_id,
                  'received_at': obs['received_at'], 'paper_eligible': False,
                  'status': 'OBSERVED_UNQUALIFIED', 'source_updated_at': None}
        try:
            parsed = PARSERS[kind](raw, race_id)
            adapter.verify_parsed(parsed, receipt)
            report.update(parsed)
        except (ValueError, UnicodeError, csv.Error):
            report.update(status='QUARANTINED', reason='PAGE_UNQUALIFIED')
        report['parsed_at'] = stamp(self.clock())
        digest = await self.save_body(canonical(report))
        await self.run('INSERT OR IGNORE INTO page_parses VALUES(?,?,?,?,?,?,NULL,?)',
                       parse_id, observation_id, kind, race_id, VERSIONS[kind], report['parsed_at'], digest)
        await self.run(f'''UPDATE page_parses SET available_at={PUBLICATION_CLOCK}
            WHERE parse_id=? AND available_at IS NULL AND parsed_at<={PUBLICATION_CLOCK}''', parse_id)
        result = await self.first('SELECT * FROM page_parses WHERE parse_id=?', parse_id)
        if not result['available_at']:
            raise ValueError('CLOCK_ORDER')
        return result

    async def asof(self, kind, race_id, at):
        if kind not in VERSIONS:
            raise ValueError('PAGE_KIND')
        cutoff = self.cutoff(at)
        row = await self.first('''SELECT p.*,o.received_at FROM page_parses p
            JOIN raw_observations o USING(observation_id)
            WHERE p.kind=? AND p.race_id=? AND p.available_at<=?
            ORDER BY o.received_at DESC,p.available_at DESC,p.parse_id DESC LIMIT 1''', kind, race_id, cutoff)
        evidence = {**await self.read_body(row['body_hash']), 'available_at': row['available_at']} if row else None
        return {'race_id': race_id, 'asof_at': cutoff, 'evidence': evidence,
                'age_seconds': seconds(cutoff, evidence['received_at']) if evidence else None,
                'paper_eligible': False, 'availability_clock': 'D1_PUBLICATION_STATEMENT_UTC'}

    async def history(self, kind, race_id, at, cursor=None, limit=50):
        if kind not in VERSIONS or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('PAGE_KIND_OR_LIMIT')
        cutoff = self.cutoff(at)
        lower = ''
        if cursor is not None:
            old = await self.first('''SELECT available_at FROM page_parses
                WHERE parse_id=? AND kind=? AND race_id=? AND available_at<=?''', cursor, kind, race_id, cutoff)
            if not old:
                raise ValueError('CURSOR')
            lower = old['available_at']
        rows = await self.all('''SELECT * FROM page_parses WHERE kind=? AND race_id=? AND available_at<=?
            AND (available_at,parse_id)>(?,?) ORDER BY available_at,parse_id LIMIT ?''',
            kind, race_id, cutoff, lower, cursor or '', limit + 1)
        items, size = [], 0
        for row in rows[:limit]:
            item = {**await self.read_body(row['body_hash']), 'available_at': row['available_at']}
            length = len(canonical(item))
            if length > HISTORY_PAGE_BYTES:
                raise ValueError('HISTORY_ITEM_LIMIT')
            if size + length > HISTORY_PAGE_BYTES:
                break
            items.append(item)
            size += length
        return {'asof_at': cutoff, 'history': items,
                'next_cursor': items[-1]['id'] if len(rows) > len(items) else None, 'paper_eligible': False}
