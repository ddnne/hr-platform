"""Fixed as-of research over saved R2/D1 observations. No fetch or Paper writes."""
from datetime import timedelta
import math
import re
from uuid import uuid4

from .cloud_history import CloudHistory, PUBLICATION_CLOCK
from .cloud_pages import CloudPages
from .cloud_race_files import CloudRaceFiles
from .common import canonical, identity, instant, seconds, sha, stamp, utcnow
from .history import asof_view
from .paper_rules import validate_paper_config
from .prospective_rules import configuration, metadata_reason, qualify_observed

FAMILIES = ('successors', 'portfolio', 'portfolio-scenarios', 'expansions',
            'alternatives', 'kelly', 'joint-kelly', 'all-markets-kelly')


class CloudResearch(CloudHistory):
    body_prefix = 'research-records'

    def __init__(self, bucket, database, clock=utcnow, *, storage_policy=None, research_policy, engine_id):
        super().__init__(bucket, database, clock, storage_policy=storage_policy)
        if (set(research_policy) != {'version', 'lease_seconds', 'max_attempts', 'max_pending_jobs',
                                    'max_rpc_bytes', 'list_limit', 'trend_lookback_seconds'}
                or research_policy['version'] != 'cloud-research-v1'
                or any(type(v) is not int or v <= 0 for k, v in research_policy.items() if k != 'version')
                or not re.fullmatch('[0-9a-f]{64}', engine_id)):
            raise ValueError('RESEARCH_POLICY')
        self.policy, self.engine_id = research_policy, engine_id
        args = {'clock': clock, 'storage_policy': storage_policy}
        self.odds = CloudHistory(bucket, database, **args)
        self.races = CloudRaceFiles(bucket, database, **args)
        self.pages = CloudPages(bucket, database, **args)

    async def register(self, version, base, configs):
        from .research_suite import strategy_names
        if not isinstance(version, str) or not re.fullmatch('[A-Za-z0-9_.:-]+', version):
            raise ValueError('BUNDLE_VERSION')
        config = configuration(base)
        validate_paper_config(config)
        if config['target'] != 'quinella' or config['references'] != ['win', 'exacta'] or set(configs) != set(FAMILIES):
            raise ValueError('NAR_RESEARCH_CONFIG')
        names = strategy_names(configs)
        if len(set(names)) != len(names):
            raise ValueError('STRATEGY_NAMES')
        bundle = {'version': version, 'engine_id': self.engine_id, 'base': config, 'configs': configs,
                  'strategy_names': names, 'research_policy': self.policy,
                  'purpose': 'RETROSPECTIVE_FIXED_ASOF_RESEARCH_NO_PAPER'}
        digest = await self.save_body(canonical(bundle))
        await self.run(f'''INSERT OR IGNORE INTO cloud_research_bundles
            VALUES(?,?,?,?,{PUBLICATION_CLOCK})''', digest, version, self.engine_id, digest)
        row = await self.first('SELECT * FROM cloud_research_bundles WHERE version=?', version)
        if row['bundle_id'] != digest:
            raise ValueError('BUNDLE_VERSION_CONFLICT')
        return row

    async def enqueue(self, bundle_id, race_id, at):
        row = await self.first('SELECT * FROM cloud_research_bundles WHERE bundle_id=?', bundle_id)
        if not row or row['engine_id'] != self.engine_id:
            raise ValueError('BUNDLE_OR_ENGINE')
        if not isinstance(race_id, str) or not re.fullmatch(r'\d{8}:[^:]+:[1-9]\d*', race_id):
            raise ValueError('RACE_ID')
        cutoff = self.cutoff(at)
        key = identity([bundle_id, race_id, cutoff])
        await self.run(f'''INSERT OR IGNORE INTO cloud_research_jobs
            (job_id,bundle_id,race_id,asof_at,registered_at)
            SELECT ?,?,?,?,{PUBLICATION_CLOCK}
            WHERE (SELECT count(*) FROM cloud_research_jobs WHERE status IN ('QUEUED','RUNNING'))<?''',
            key, bundle_id, race_id, cutoff, self.policy['max_pending_jobs'])
        row = await self.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', key)
        if not row:
            raise ValueError('RESEARCH_QUEUE_FULL')
        return row

    async def jobs(self, bundle_id, after=None):
        return await self.all('''SELECT * FROM cloud_research_jobs WHERE bundle_id=? AND job_id>?
            ORDER BY job_id LIMIT ?''', bundle_id, after or '', self.policy['list_limit'])

    async def final_prices(self, race_id, markets, at):
        return await self.odds.final_prices(race_id, markets, at)

    async def result(self, job_id, at):
        cutoff = self.cutoff(at)
        row = await self.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', job_id)
        if row and row['registered_at'] > cutoff:
            row = None
        published = row and row['available_at'] and row['available_at'] <= cutoff
        index = row if published else ({k: row[k] for k in
                 ('job_id', 'bundle_id', 'race_id', 'asof_at', 'registered_at')} if row else None)
        return {'job': index, 'result': await self.read_body(row['result_hash']) if published and row['result_hash'] else None,
                'asof_at': cutoff, 'paper_eligible': False}

    async def evaluate(self, job_id, at):
        """Publish a separate evaluation of frozen candidates; never refit them."""
        from .research_evaluation import VERSION, evaluate_candidates, qualified_payout
        from .race_state import MAX_BYTES
        available = await self.result(job_id, at)
        job, result = available['job'], available['result']
        if not result or job['status'] != 'COMPLETE':
            raise ValueError('RESEARCH_RESULT_UNAVAILABLE')
        saved = await self.read_body(job['input_hash'])
        bundle_row = await self.first('SELECT * FROM cloud_research_bundles WHERE bundle_id=?', job['bundle_id'])
        bundle = await self.read_body(bundle_row['body_hash'])
        markets = sorted(saved['view']['markets'])
        prices = (await self.odds.final_prices(job['race_id'], markets, at) if markets else {'markets': {}})
        evidence = (await self.pages.asof('payout', job['race_id'], at))['evidence']
        basis = {'version': VERSION, 'evaluator_engine_id': self.engine_id,
                 'job_id': job_id, 'result_hash': job['result_hash'],
                 'input_hash': job['input_hash'], 'payout': evidence,
                 'final_prices': prices['markets']}
        evaluation_id = identity(basis)
        existing = await self.first('SELECT * FROM cloud_research_evaluations WHERE evaluation_id=?', evaluation_id)
        if existing:
            return {'record': existing, 'evaluation': await self.read_body(existing['body_hash'])}
        payout, payout_basis = {}, 'PAYOUT_UNAVAILABLE_OR_UNQUALIFIED'
        if (evidence and evidence['status'] == 'PAYOUT_QUALIFIED'
                and evidence['available_at'] >= result['asof_at']):
            raw = await self.pages.body(f"raw/{evidence['raw_hash']}", MAX_BYTES)
            if sha(raw) != evidence['raw_hash']:
                raise ValueError('BODY_CORRUPT')
            metadata = saved['metadata']['evidence']
            if metadata and metadata.get('metadata'):
                horses = metadata['metadata']['horses']
                places = 2 if len(horses) <= bundle['configs']['all-markets-kelly']['place_two_paid_max_runners'] else 3
                payout, payout_basis = qualified_payout(raw, evidence, job['race_id'], horses, markets, places)
        evaluation = {**basis, 'evidence_asof_at': available['asof_at'],
            'strategies': evaluate_candidates(result, saved, bundle['base'], payout, payout_basis, prices),
            'payout_basis': payout_basis, 'complete_payout_markets': payout.get('complete_markets', []),
            'paper_eligible': False, 'timing_qualified': False, 'daily_budget_applied': False,
            'purpose': 'RETROSPECTIVE_FIXED_CANDIDATES_NOT_LIVE_PAPER',
            'price_basis': 'STAKE_WEIGHTED_DISPLAYED_BOUNDS_NOT_INFERRED_FROM_PAYOUT'}
        digest = await self.save_body(canonical(evaluation))
        await self.run(f'''INSERT OR IGNORE INTO cloud_research_evaluations
            VALUES(?,?,?,?,{PUBLICATION_CLOCK})''', evaluation_id, job_id, VERSION, digest)
        row = await self.first('SELECT * FROM cloud_research_evaluations WHERE evaluation_id=?', evaluation_id)
        return {'record': row, 'evaluation': await self.read_body(row['body_hash'])}

    async def evaluation(self, evaluation_id, at):
        cutoff = self.cutoff(at)
        row = await self.first('''SELECT * FROM cloud_research_evaluations
            WHERE evaluation_id=? AND available_at<=?''', evaluation_id, cutoff)
        return {'record': row, 'evaluation': await self.read_body(row['body_hash']) if row else None,
                'asof_at': cutoff, 'paper_eligible': False}

    async def input(self, job, bundle):
        config, at, race = bundle['base'], job['asof_at'], job['race_id']
        all_markets = bundle['configs']['all-markets-kelly']['markets']
        observed = await self.odds.asof(race, all_markets, at, config['max_age_seconds'])
        fresh = {m: r for m, r in observed['markets'].items() if 0 <= r['age_seconds'] <= config['max_age_seconds']}
        # Retain the existing conservative observation boundary. Receipt proximity
        # is not proof of synchronous provider updates across different files.
        latest = max(fresh.values(), key=lambda r: instant(r['received_at']), default=None)
        fresh = {m: r for m, r in fresh.items() if r['observation_id'] == latest['observation_id']}
        view = asof_view({m: [r] for m, r in fresh.items()}, list(fresh), at, config['max_age_seconds'])
        if not fresh:
            view['reason'] = 'DATA_MISSING'
        meta, state = await self.races.metadata(race, at), await self.pages.asof('state', race, at)
        reason = metadata_reason(race, meta['evidence'])
        schedule = None
        if not reason:
            start = stamp(meta['evidence']['metadata']['scheduled_start_at'])
            schedule = {'version': identity([race, start]), 'scheduled_start_at': start,
                        'known_at': meta['evidence']['available_at'], 'sales_close_at': None}
            view = qualify_observed(view, meta, state, race, schedule, config, at)
        else:
            view['reason'] = reason
        before = stamp((instant(at) - timedelta(seconds=bundle['research_policy']['trend_lookback_seconds'])).isoformat())
        previous = await self.odds.asof(race, [config['target']], before, config['max_age_seconds'])
        return {'view': view, 'observed_view': observed, 'metadata': meta, 'state': state, 'schedule': schedule,
                'previous': previous, 'input_mode': 'RETROSPECTIVE_FIXED_ASOF_NOT_LIVE_PAPER'}

    async def tick(self, analyzer=None):
        # A timeout leaves the lease visible; configured bounded attempts cannot loop forever.
        await self.run(f'''UPDATE cloud_research_jobs SET status='FAILED',error_code='ATTEMPTS_EXHAUSTED',
            available_at={PUBLICATION_CLOCK},owner=NULL,lease_until=NULL
            WHERE status='RUNNING' AND lease_until<={PUBLICATION_CLOCK} AND attempts>=?''', self.policy['max_attempts'])
        row = await self.first(f'''SELECT j.*,b.engine_id,b.body_hash FROM cloud_research_jobs j
            JOIN cloud_research_bundles b USING(bundle_id)
            WHERE (j.status='QUEUED' OR (j.status='RUNNING' AND j.lease_until<={PUBLICATION_CLOCK}))
            AND j.attempts<? ORDER BY j.registered_at,j.job_id LIMIT 1''', self.policy['max_attempts'])
        if not row:
            return {'status': 'IDLE'}
        if row['engine_id'] != self.engine_id:
            await self.run(f"""UPDATE cloud_research_jobs SET status='ENGINE_UNAVAILABLE',
                available_at={PUBLICATION_CLOCK},owner=NULL,lease_until=NULL WHERE job_id=?
                AND (status='QUEUED' OR (status='RUNNING' AND lease_until<={PUBLICATION_CLOCK}))""", row['job_id'])
            return {'status': 'ENGINE_UNAVAILABLE', 'job_id': row['job_id']}
        owner = str(uuid4())
        now = (await self.first(f'SELECT {PUBLICATION_CLOCK} AS now'))['now']
        lease = stamp((instant(now) + timedelta(seconds=self.policy['lease_seconds'])).isoformat())
        await self.run(f'''UPDATE cloud_research_jobs SET status='RUNNING',owner=?,attempts=attempts+1,
            started_at={PUBLICATION_CLOCK},lease_until=?
            WHERE job_id=? AND (status='QUEUED' OR (status='RUNNING' AND lease_until<={PUBLICATION_CLOCK}))
            AND attempts<?''', owner, lease, row['job_id'], self.policy['max_attempts'])
        job = await self.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', row['job_id'])
        if job['owner'] != owner:
            return {'status': 'BUSY', 'job_id': row['job_id']}
        try:
            bundle = await self.read_body(row['body_hash'])
            saved = await self.input(job, bundle)
            input_hash = await self.save_body(canonical(saved))
            if saved['view']['reason']:
                output = {'candidates': {n: {'status': 'INPUT_EXCLUDED', 'reason': saved['view']['reason'],
                                            'tickets': [], 'stake_yen': 0} for n in bundle['strategy_names']}}
            else:
                if analyzer is None:
                    from .research_suite import analyze_suite
                    analyzer = analyze_suite
                metadata = saved['metadata']['evidence']['metadata']
                markets = {m: r['content'] for m, r in saved['view']['markets'].items()}
                options = trend_options(saved, bundle['base'])
                output = analyzer(sorted(map(int, metadata['horses'])), markets, bundle['base'], bundle['configs'],
                    venue=job['race_id'].split(':')[1], frames={int(h): int(v['frame']) for h, v in metadata['horses'].items()},
                    **options)
            if set(output['candidates']) != set(bundle['strategy_names']):
                raise ValueError('STRATEGY_REGISTRY_MISMATCH')
            result_hash = await self.save_body(canonical({'job_id': job['job_id'], 'bundle_id': job['bundle_id'],
                'engine_id': self.engine_id, 'race_id': job['race_id'], 'asof_at': job['asof_at'],
                'input_hash': input_hash, 'output': output, 'paper_eligible': False,
                'timing_qualified': False, 'daily_budget_applied': False, 'settlement_status': 'NOT_EVALUATED',
                'computation_basis': 'SAVED_AVAILABLE_AT_ASOF_NOT_FINAL_ODDS'}))
            await self.run(f'''UPDATE cloud_research_jobs SET status='COMPLETE',input_hash=?,result_hash=?,
                available_at={PUBLICATION_CLOCK},owner=NULL,lease_until=NULL
                WHERE job_id=? AND owner=? AND status='RUNNING' AND lease_until>{PUBLICATION_CLOCK}''',
                input_hash, result_hash, job['job_id'], owner)
        except Exception:
            # Original and scientific details stay private; public logs contain no data.
            await self.run(f'''UPDATE cloud_research_jobs SET status='FAILED',error_code='RESEARCH_RUNTIME_ERROR',
                available_at={PUBLICATION_CLOCK},owner=NULL,lease_until=NULL WHERE job_id=? AND owner=?''', job['job_id'], owner)
        return await self.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', job['job_id'])


def trend_options(saved, config):
    current = saved['view']['markets'].get(config['target'])
    previous = saved['previous']['markets'].get(config['target'])
    result = {'previous_quotes': None, 'elapsed_seconds': None}
    if not current or not previous or saved['previous']['reason']:
        return result
    quotes = previous['content']['quotes']
    if (previous['observation_id'] == current['observation_id']
            or seconds(current['received_at'], previous['received_at']) <= 0
            or set(quotes) != set(current['content']['quotes'])
            or previous['content']['state']['status'] not in {'UNKNOWN', 'PRE_RACE'}
            or any(q['display_status'] != 'FIXED' or type(q['odds']) not in {int, float}
                   or not math.isfinite(q['odds']) or q['odds'] < 1 for q in quotes.values())):
        return result
    return {'previous_quotes': {s: q['odds'] for s, q in quotes.items()},
            'elapsed_seconds': seconds(current['received_at'], previous['received_at'])}
