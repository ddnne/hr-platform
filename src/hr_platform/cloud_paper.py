"""Private, prospectively enrolled Paper on R2/D1. No provider or betting access.

One D1 statement freezes all three models with a common completion clock and
atomic daily limits. Failed/late computations consume the fixed decision as NO_BET.
"""
import asyncio
import json
from datetime import timedelta
from uuid import uuid4
from .cloud_history import CloudHistory, PUBLICATION_CLOCK
from .cloud_race_files import CloudRaceFiles
from .cloud_pages import CloudPages
from .common import canonical, identity, instant, paper_asof, seconds, stamp, utcnow
from .paper_rules import eligibility, select, settlement_values, validate_paper_config
from .prospective_rules import configuration, metadata_reason, qualify


class CloudPaper(CloudHistory):
    body_prefix = 'paper-records'

    def __init__(self, bucket, database, clock=utcnow, *, storage_policy=None, paper_policy):
        super().__init__(bucket, database, clock, storage_policy=storage_policy)
        if (set(paper_policy) != {'version', 'lease_seconds', 'max_future_seconds', 'max_pending_plans',
                                  'decision_lookahead_seconds', 'alarm_lead_seconds'}
                or paper_policy['version'] != 'cloud-paper-v1'
                or any(type(paper_policy[k]) is not int or paper_policy[k] <= 0 for k in paper_policy if k != 'version')):
            raise ValueError('CLOUD_PAPER_POLICY')
        if paper_policy['alarm_lead_seconds'] > paper_policy['decision_lookahead_seconds']:
            raise ValueError('CLOUD_PAPER_ALARM_LEAD')
        self.policy = paper_policy
        self.database_time = ''
        args = {'clock': self.evidence_clock, 'storage_policy': storage_policy}
        self.odds = CloudHistory(bucket, database, **args)
        self.races = CloudRaceFiles(bucket, database, **args)
        self.pages = CloudPages(bucket, database, **args)

    async def now(self):
        now = (await self.first(f'SELECT {PUBLICATION_CLOCK} AS now'))['now']
        self.database_time = max(self.database_time, now)
        return now

    def evidence_clock(self):
        # D1 publication and Worker clocks can differ slightly. A time already
        # read from D1 is an observed bound, not a client-supplied future cutoff.
        return max(stamp(self.clock()), self.database_time)

    async def metadata(self, race_id, at):
        saved = await self.races.day(race_id.split(':')[0], at, race_id)
        row = saved['snapshot']
        evidence = None if not row else {
            'id': row['parse_id'], 'observation_id': row['observation_id'], 'raw_hash': row['raw_sha256'],
            'received_at': row['received_at'], 'available_at': row['available_at'],
            'status': 'OBSERVED_UNQUALIFIED', 'metadata': saved['races'].get(race_id),
        }
        return {'race_id': race_id, 'asof_at': stamp(at), 'evidence': evidence,
                'age_seconds': seconds(at, row['received_at']) if row else None}

    async def enroll(self, race_id, base_config):
        config = configuration(base_config)
        return await self.register(race_id, config)

    async def register(self, race_id, config):
        validate_paper_config(config)
        if config['comparison_models'] != ['direct', 'marginal', 'reference']:
            raise ValueError('COMPARISON_MODELS')
        digest, experiment = identity(config), config['version']
        await self.run('INSERT OR IGNORE INTO cloud_paper_experiments VALUES(?,?)', experiment, digest)
        if (await self.first('SELECT config_hash FROM cloud_paper_experiments WHERE experiment=?', experiment))['config_hash'] != digest:
            raise ValueError('EXPERIMENT_CONFIG_CHANGED')
        key = identity([experiment, race_id])
        old = await self.first('SELECT * FROM cloud_paper_plans WHERE plan_id=?', key)
        if old and old['decisions']:
            return await self.plan(key)
        now = await self.now()
        try:
            meta = await self.metadata(race_id, now)
        except Exception:
            if old:
                return await self.plan(key)
            raise
        reason = metadata_reason(race_id, meta['evidence'])
        if reason:
            if old:
                return await self.plan(key)
            raise ValueError(reason)
        evidence = meta['evidence']
        start = stamp(evidence['metadata']['scheduled_start_at'])
        previous = json.loads(old['plan_body']) if old else None
        if previous and previous['schedule']['scheduled_start_at'] == start:
            return await self.plan(key)
        schedule = {'version': identity([race_id, start]), 'scheduled_start_at': start,
                    'known_at': evidence['available_at'], 'sales_close_at': None}
        at = paper_asof(schedule, config, race_id)
        if not old and not 0 < seconds(at, now) <= self.policy['max_future_seconds']:
            raise ValueError('PROSPECTIVE_ENROLLMENT_TIME')
        plan = {'id': key, 'revision_id': identity([key, evidence['id'], schedule,
                                                  previous['revision_id'] if previous else None]), 'race_id': race_id,
                'asof_at': at, 'schedule': schedule, 'config': config,
                'metadata_parse_id': evidence['id'],
                'supersedes': previous['revision_id'] if previous else None,
                'change_reason': 'SCHEDULE_CHANGED' if previous else 'INITIAL_ENROLLMENT'}
        body = canonical(plan).decode()
        if old:
            await self.run(f'''UPDATE cloud_paper_plans SET asof_at=?,plan_body=?,registered_at={PUBLICATION_CLOCK},
                owner=NULL,lease_until=NULL WHERE plan_id=? AND plan_body=? AND decisions IS NULL
                AND (owner IS NULL OR lease_until<={PUBLICATION_CLOCK})''', at, body, key, old['plan_body'])
        else:
            await self.run(f'''INSERT OR IGNORE INTO cloud_paper_plans
                (plan_id,experiment,race_id,day,asof_at,plan_body,registered_at)
                SELECT ?,?,?,?,?,?,{PUBLICATION_CLOCK} WHERE {PUBLICATION_CLOCK}<?
                AND (SELECT count(*) FROM cloud_paper_plans WHERE decisions IS NULL)<?''',
                key, experiment, race_id, race_id.split(':')[0], at, body, at, self.policy['max_pending_plans'])
        return await self.plan(key)

    async def plan(self, plan_id, at=None):
        row = await self.first('''SELECT * FROM cloud_paper_plan_revisions WHERE plan_id=? AND registered_at<=?
            ORDER BY registered_at DESC,rowid DESC LIMIT 1''', plan_id, stamp(at) if at else await self.now())
        if not row:
            raise ValueError('PLAN_NOT_REGISTERED')
        return {**json.loads(row['plan_body']), 'registered_at': row['registered_at']}

    async def decision(self, plan_id):
        row = await self.first('SELECT decisions,details_hash,decision_at FROM cloud_paper_plans WHERE plan_id=?', plan_id)
        return {'plan_id': plan_id, 'decisions': json.loads(row['decisions']) if row and row['decisions'] else None,
                'details_hash': row['details_hash'] if row else None,
                'decision_at': row['decision_at'] if row else None}

    async def decide(self, plan_id, analyzer=None, *, refresh=True):
        from .cloud_model import execute
        analyzer = analyzer or execute
        old = await self.decision(plan_id)
        if old['decisions'] is not None:
            return old
        plan = await self.plan(plan_id)
        if refresh:
            plan = await self.register(plan['race_id'], plan['config'])
        now = await self.now()
        if now < plan['asof_at']:
            return {'status': 'NOT_DUE', 'plan_id': plan_id}
        owner = str(uuid4())
        lease = stamp((instant(now) + timedelta(seconds=self.policy['lease_seconds'])).isoformat())
        await self.run('''UPDATE cloud_paper_plans SET owner=?,lease_until=? WHERE plan_id=?
            AND json_extract(plan_body,'$.revision_id')=?
            AND decisions IS NULL AND (owner IS NULL OR lease_until<=?)''',
            owner, lease, plan_id, plan['revision_id'], now)
        if (await self.first('SELECT owner FROM cloud_paper_plans WHERE plan_id=?', plan_id))['owner'] != owner:
            return {'status': 'BUSY', 'plan_id': plan_id}
        race, config, at, schedule = plan['race_id'], plan['config'], plan['asof_at'], plan['schedule']
        reference_policy = validate_paper_config(config)
        deadline = stamp((instant(at) + timedelta(seconds=config['max_decision_delay_seconds'])).isoformat())
        if schedule.get('sales_close_at'):
            deadline = min(deadline, stamp(schedule['sales_close_at']))
        view = {'reason': 'DECISION_TOO_LATE' if now > deadline else 'INPUT_UNAVAILABLE', 'markets': {}}
        if now <= deadline:
            try:
                view = await self.odds.asof(race, [config['target'], *config['references']], at, config['max_age_seconds'])
                view = qualify(view, await self.metadata(race, at), await self.pages.asof('state', race, at),
                               plan, race, schedule, config, at)
            except Exception:
                view = {'reason': 'INPUT_UNAVAILABLE', 'markets': {}}
        reason = view['reason'] or eligibility(view, config, schedule, now, race_id=race)
        envelope, analysis = None, None
        assumptions = list(view.get('research_assumptions', []))
        if reason is None:
            content = {h: x['content'] for h, x in view['markets'].items()}
            try:
                envelope = json.loads(analyzer(canonical({'runners': content[config['target']]['state']['runners'],
                                                          'markets': content, 'config': config}).decode()))
                analysis = envelope.get('analysis')
            except Exception:
                envelope = {'status': 'MODEL_ERROR'}
            if analysis is None:
                reason = 'DATA_MISSING' if envelope.get('status') == 'DATA_MISSING' else 'MODEL_ERROR'
            elif analysis['identification']['status'] == 'INCONSISTENT':
                if reference_policy == 'require_feasible':
                    reason = 'REFERENCE_INCONSISTENT'
                else:
                    assumptions.append('INCONSISTENT_REFERENCES_SOFT_CALIBRATION')
        try:
            details = await self.save_body(canonical({'plan': plan, 'input_view': view, 'model': envelope}))
        except Exception:
            details, reason = None, reason or 'DETAILS_UNAVAILABLE'
        candidates = []
        for model in config['comparison_models']:
            chosen = select(analysis['rows'], model, config['tie_tolerance']) if analysis and not reason else None
            candidates.append({'id': identity([config['version'], model, race]), 'experiment': config['version'],
                'model': model, 'race_id': race, 'target': config['target'], 'mode': config['mode'],
                'config_hash': identity(config), 'reason': reason or (None if chosen else 'NO_EDGE'),
                'selection': chosen['selection'] if chosen else None, 'stake_yen': config['stake_yen'] if chosen else 0,
                'asof_at': at, 'decision_started_at': now, 'details_hash': details, 'research_assumptions': assumptions,
                'freshness_basis': config['freshness_basis'], 'paper_timing_assumption': config['paper_timing_assumption'],
                'reference_constraint_policy': reference_policy, 'real_stake_yen': 0})
        # SQLite's 'now' is shared within this statement. The writer lock also
        # serializes the budget read and all three immutable ledger records.
        await self.run(f'''WITH candidates AS (
            SELECT value,CASE WHEN {PUBLICATION_CLOCK}>? THEN 'DECISION_TOO_LATE'
              WHEN COALESCE((SELECT sum(json_extract(d.value,'$.stake_yen'))
                FROM cloud_paper_plans p,json_each(p.decisions) d
                WHERE p.experiment=? AND p.day=? AND json_extract(d.value,'$.model')=json_extract(c.value,'$.model')),0)
                + json_extract(c.value,'$.stake_yen')>? THEN 'DAILY_LIMIT'
              ELSE json_extract(value,'$.reason') END reason FROM json_each(?) c)
            UPDATE cloud_paper_plans SET details_hash=?,decision_at={PUBLICATION_CLOCK},decisions=(
              SELECT json_group_array(json_set(value,'$.decision_at',{PUBLICATION_CLOCK},
                '$.reason',reason,'$.status',CASE WHEN reason IS NULL THEN 'PAPER_BET' ELSE 'NO_BET' END,
                '$.selection',CASE WHEN reason IS NULL THEN json_extract(value,'$.selection') ELSE NULL END,
                '$.stake_yen',CASE WHEN reason IS NULL THEN json_extract(value,'$.stake_yen') ELSE 0 END)) FROM candidates)
            WHERE plan_id=? AND owner=? AND decisions IS NULL''',
            deadline, config['version'], race.split(':')[0], config['daily_stake_limit_yen_per_model'],
            canonical(candidates).decode(), details, plan_id, owner)
        return await self.decision(plan_id)

    async def settle(self, plan_id):
        decision = await self.decision(plan_id)
        if decision['decisions'] is None:
            return {'status': 'DECISION_PENDING'}
        race = decision['decisions'][0]['race_id']
        now = await self.now()
        evidence = (await self.pages.asof('payout', race, now))['evidence']
        if not evidence or evidence['status'] != 'PAYOUT_QUALIFIED' or evidence['available_at'] < decision['decision_at']:
            return {'status': 'PAYOUT_PENDING'}
        old = await self.first('SELECT * FROM cloud_paper_settlements WHERE plan_id=? AND evidence_id=?', plan_id, evidence['id'])
        if old:
            return {**await self.read_body(old['body_hash']), 'recorded_at': old['available_at']}
        records = []
        for row in decision['decisions']:
            if row['stake_yen'] and (row['target'] not in evidence['complete_markets'] or any(
                h not in evidence['runners'] or evidence['runners'][h]['status'] == 'CANCELLED_BEFORE_SALES'
                for h in row['selection'].split('-'))):
                return {'status': 'PAYOUT_PENDING'}
            records.append({'decision_id': row['id'], 'revision': evidence['id'],
                            'available_at': evidence['available_at'], **settlement_values(row, evidence)})
        result = {'plan_id': plan_id, 'evidence': evidence, 'settlements': records, 'status': 'RECORDED'}
        digest = await self.save_body(canonical(result))
        await self.run(f'INSERT OR IGNORE INTO cloud_paper_settlements VALUES(?,?,?,{PUBLICATION_CLOCK})', plan_id, evidence['id'], digest)
        saved = await self.first('SELECT * FROM cloud_paper_settlements WHERE plan_id=? AND evidence_id=?', plan_id, evidence['id'])
        return {**await self.read_body(saved['body_hash']), 'recorded_at': saved['available_at']}

    async def next_alarm(self):
        """Use the current persisted plan and lease; no second scheduling ledger."""
        now = await self.now()
        rows = await self.all('''SELECT asof_at,owner,lease_until FROM cloud_paper_plans
            WHERE decisions IS NULL''')
        times = [max(instant(row['asof_at']) - timedelta(seconds=self.policy['alarm_lead_seconds']),
                     instant(row['lease_until']) if row['owner'] and row['lease_until'] else instant(now))
                 for row in rows]
        return max(round(instant(now).timestamp() * 1000) + 1,
                   round(min(times).timestamp() * 1000)) if times else None

    async def tick(self, *, wait_for_due=False):
        pending = await self.all('SELECT plan_id,plan_body FROM cloud_paper_plans WHERE decisions IS NULL')
        for row in pending:
            plan = json.loads(row['plan_body'])
            await self.register(plan['race_id'], plan['config'])
        now = await self.now()
        due = await self.first('''SELECT plan_id FROM cloud_paper_plans WHERE decisions IS NULL AND asof_at<=?
            AND (owner IS NULL OR lease_until<=?) ORDER BY asof_at,plan_id LIMIT 1''', now, now)
        if due:
            return await self.decide(due['plan_id'], refresh=False)
        if wait_for_due:
            # Cron can start partway through a minute. Discover the next fixed
            # decision before its minute, then wait locally without changing as-of.
            horizon = stamp((instant(now) + timedelta(seconds=self.policy['decision_lookahead_seconds'])).isoformat())
            upcoming = await self.first('''SELECT plan_id,asof_at FROM cloud_paper_plans
                WHERE decisions IS NULL AND asof_at>? AND asof_at<=?
                AND (owner IS NULL OR lease_until<=?) ORDER BY asof_at,plan_id LIMIT 1''', now, horizon, now)
            if upcoming:
                await asyncio.sleep(max(0, seconds(upcoming['asof_at'], await self.now())))
                # A revised schedule can postpone/cancel this attempt. Actual
                # completion time still enforces the original decision deadline.
                return await self.decide(upcoming['plan_id'])
        pending = await self.first('''SELECT p.plan_id FROM cloud_paper_plans p WHERE p.decisions IS NOT NULL
            AND (NOT EXISTS(SELECT 1 FROM cloud_paper_settlements s WHERE s.plan_id=p.plan_id)
              OR EXISTS(SELECT 1 FROM page_parses e WHERE e.kind='payout' AND e.race_id=p.race_id
                AND e.available_at>=p.decision_at AND NOT EXISTS(SELECT 1 FROM cloud_paper_settlements s
                  WHERE s.plan_id=p.plan_id AND s.evidence_id=e.parse_id)))
            ORDER BY coalesce(p.settlement_checked_at,''),p.asof_at,p.plan_id LIMIT 1''')
        if pending:
            await self.run(f'UPDATE cloud_paper_plans SET settlement_checked_at={PUBLICATION_CLOCK} WHERE plan_id=?', pending['plan_id'])
        return await self.settle(pending['plan_id']) if pending else {'status': 'IDLE'}

    async def history(self, plan_id, at):
        cutoff = self.cutoff(at)
        plan = await self.first('''SELECT * FROM cloud_paper_plans WHERE plan_id=? AND EXISTS(
            SELECT 1 FROM cloud_paper_plan_revisions r WHERE r.plan_id=cloud_paper_plans.plan_id AND r.registered_at<=?)''',
            plan_id, cutoff)
        if not plan:
            return {'plan_id': plan_id, 'asof_at': cutoff, 'decisions': None, 'settlements': []}
        rows = await self.all('''SELECT * FROM cloud_paper_settlements WHERE plan_id=? AND available_at<=?
            ORDER BY available_at,evidence_id''', plan_id, cutoff)
        return {'plan_id': plan_id, 'asof_at': cutoff,
                'decisions': json.loads(plan['decisions']) if plan['decision_at'] and plan['decision_at'] <= cutoff else None,
                'settlements': [{**await self.read_body(row['body_hash']), 'recorded_at': row['available_at']} for row in rows]}
