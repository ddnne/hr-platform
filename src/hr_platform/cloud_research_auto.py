"""Automatic research over saved schedules and fixed candidates. No fetching."""
from datetime import date, timedelta
from uuid import uuid4

from .cloud_history import PUBLICATION_CLOCK
from .common import JST, canonical, identity, instant, paper_asof, stamp
from .prospective_rules import metadata_reason


async def activate(engine, bundle_id, from_date):
    if engine.policy['version'] != 'cloud-research-v2':
        raise ValueError('AUTOMATIC_POLICY_REQUIRED')
    date.fromisoformat(f'{from_date[:4]}-{from_date[4:6]}-{from_date[6:]}')
    if len(from_date) != 8:
        raise ValueError('START_DATE')
    row = await engine.first('SELECT * FROM cloud_research_bundles WHERE bundle_id=?', bundle_id)
    if not row or row['engine_id'] != engine.engine_id:
        raise ValueError('BUNDLE_OR_ENGINE')
    await engine.run(f'''INSERT INTO cloud_research_automatic(slot,bundle_id,from_date,activated_at)
        VALUES(1,?,?,{PUBLICATION_CLOCK}) ON CONFLICT(slot) DO UPDATE SET
        bundle_id=excluded.bundle_id,from_date=excluded.from_date,activated_at=excluded.activated_at,
        schedule_owner=NULL,schedule_lease_until=NULL
        WHERE bundle_id<>excluded.bundle_id OR from_date<>excluded.from_date''', bundle_id, from_date)
    return await engine.first('SELECT * FROM cloud_research_automatic WHERE slot=1')


async def schedule_registered(engine):
    active = await engine.first('''SELECT a.*,b.engine_id,b.body_hash FROM cloud_research_automatic a
        JOIN cloud_research_bundles b USING(bundle_id) WHERE slot=1''')
    if not active or active['engine_id'] != engine.engine_id:
        return {'status': 'NO_ACTIVE_BUNDLE'}
    owner = str(uuid4())
    db_now = (await engine.first(f'SELECT {PUBLICATION_CLOCK} AS now'))['now']
    lease = stamp((instant(db_now) + timedelta(seconds=engine.policy['lease_seconds'])).isoformat())
    claim = await engine.run(f'''UPDATE cloud_research_automatic SET schedule_owner=?,schedule_lease_until=?
        WHERE slot=1 AND bundle_id=? AND from_date=? AND activated_at=?
        AND (schedule_owner IS NULL OR schedule_lease_until<={PUBLICATION_CLOCK})''',
        owner, lease, active['bundle_id'], active['from_date'], active['activated_at'])
    if claim['meta']['changes'] != 1:
        return {'status': 'BUSY'}
    try:
        return await _schedule_claimed(engine, active, owner)
    finally:
        await engine.run('UPDATE cloud_research_automatic SET schedule_owner=NULL,schedule_lease_until=NULL '
                         'WHERE slot=1 AND schedule_owner=?', owner)


FENCE = f'''EXISTS(SELECT 1 FROM cloud_research_automatic WHERE slot=1 AND bundle_id=?
    AND schedule_owner=? AND schedule_lease_until>{PUBLICATION_CLOCK})'''


async def _schedule_claimed(engine, active, owner):
    now = stamp(engine.clock())
    day = instant(now).astimezone(JST).strftime('%Y%m%d')
    if day < active['from_date']:
        return {'status': 'NOT_DUE'}
    observed = await engine.races.schedules(day, now)
    if not observed['snapshot'] or observed['status'] != 'OK':
        return {'status': 'SCHEDULE_UNAVAILABLE'}
    bundle = await engine.read_body(active['body_hash'])
    horizon = stamp((instant(now) + timedelta(seconds=engine.policy['enrollment_horizon_seconds'])).isoformat())
    current = await engine.all('''SELECT * FROM cloud_research_jobs WHERE bundle_id=?
        AND substr(race_id,1,8)=? AND status<>'SUPERSEDED' ''', active['bundle_id'], day)
    expected = {}
    for race, meta in observed['races'].items():
        if not metadata_reason(race, {'status': 'OBSERVED_UNQUALIFIED', 'metadata': meta}):
            expected[race] = paper_asof(meta, bundle['base'], race)
    # A later schedule cannot rewrite a cutoff which has already passed, or an
    # analysis which has started. Pending future plans can follow new evidence.
    for job in current:
        if (job['schedule_hash'] and job['status'] == 'QUEUED' and not job['started_at']
                and job['asof_at'] > now and expected.get(job['race_id']) != job['asof_at']):
            await engine.run(f'''UPDATE cloud_research_jobs SET status='SUPERSEDED',available_at={PUBLICATION_CLOCK}
                WHERE job_id=? AND status='QUEUED' AND started_at IS NULL AND asof_at>{PUBLICATION_CLOCK}
                AND {FENCE}''', job['job_id'], active['bundle_id'], owner)
    pending = await engine.first("SELECT count(*) n FROM cloud_research_jobs WHERE status IN ('QUEUED','RUNNING')")
    if pending['n'] >= engine.policy['max_pending_jobs']:
        return {'status': 'IDLE', 'job_ids': []}
    registered, attempted = [], 0
    for race, at in sorted(expected.items(), key=lambda r: (r[1], r[0])):
        if not now < at <= horizon:
            continue
        old = await engine.first('''SELECT job_id FROM cloud_research_jobs
            WHERE bundle_id=? AND race_id=? AND status<>'SUPERSEDED' LIMIT 1''', active['bundle_id'], race)
        if old:
            continue
        if attempted >= engine.policy['schedule_batch_limit']:
            break
        attempted += 1
        owned = await engine.first(f'SELECT 1 AS owned WHERE {FENCE}', active['bundle_id'], owner)
        if not owned:
            break
        key = identity([active['bundle_id'], race, at])
        previous = await engine.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', key)
        meta, snapshot = observed['races'][race], observed['snapshot']
        plan = {'race_id': race, 'asof_at': at, 'scheduled_start_at': stamp(meta['scheduled_start_at']),
                'known_at': snapshot['available_at'], 'metadata_parse_id': snapshot['parse_id'],
                'observation_id': snapshot['observation_id'], 'bundle_id': active['bundle_id'],
                'supersedes_schedule_hash': previous['schedule_hash'] if previous else None,
                'purpose': 'PROSPECTIVELY_FIXED_INPUT_FOR_RETROSPECTIVE_RESEARCH'}
        digest = await engine.save_body(canonical(plan))
        # A delay can be withdrawn before either cutoff. Reuse the same logical
        # job, retaining its earlier R2 plan through this explicit hash link.
        await engine.run(f'''UPDATE cloud_research_jobs SET status='QUEUED',schedule_hash=?,available_at=NULL
            WHERE job_id=? AND status='SUPERSEDED' AND started_at IS NULL AND {PUBLICATION_CLOCK}<asof_at
            AND (SELECT count(*) FROM cloud_research_jobs WHERE status IN ('QUEUED','RUNNING'))<?
            AND NOT EXISTS(SELECT 1 FROM cloud_research_jobs j WHERE j.bundle_id=? AND j.race_id=? AND j.status<>'SUPERSEDED')
            AND {FENCE}''', digest, key, engine.policy['max_pending_jobs'], active['bundle_id'], race, active['bundle_id'], owner)
        await engine.run(f'''INSERT OR IGNORE INTO cloud_research_jobs
            (job_id,bundle_id,race_id,asof_at,registered_at,schedule_hash)
            SELECT ?,?,?,?,{PUBLICATION_CLOCK},? WHERE {PUBLICATION_CLOCK}<?
            AND (SELECT count(*) FROM cloud_research_jobs WHERE status IN ('QUEUED','RUNNING'))<?
            AND NOT EXISTS(SELECT 1 FROM cloud_research_jobs WHERE bundle_id=? AND race_id=? AND status<>'SUPERSEDED')
            AND {FENCE}''', key, active['bundle_id'], race, at, digest, at, engine.policy['max_pending_jobs'],
            active['bundle_id'], race, active['bundle_id'], owner)
        row = await engine.first('SELECT * FROM cloud_research_jobs WHERE job_id=?', key)
        if row and row['status'] == 'QUEUED':
            registered.append(key)
        if len(registered) >= engine.policy['schedule_batch_limit']:
            break
    return {'status': 'SCHEDULED' if registered else 'IDLE', 'job_ids': registered}


async def refresh_evaluations(engine):
    """Rotate fairly through completed jobs; reuse same-evidence evaluations."""
    active = await engine.first('SELECT * FROM cloud_research_automatic WHERE slot=1')
    if not active:
        return {'status': 'NO_ACTIVE_BUNDLE'}
    now = stamp(engine.clock())
    before = stamp((instant(now) - timedelta(seconds=engine.policy['evaluation_interval_seconds'])).isoformat())
    if active['evaluation_checked_at'] and active['evaluation_checked_at'] > before:
        return {'status': 'NOT_DUE'}
    query = '''SELECT job_id FROM cloud_research_jobs WHERE status='COMPLETE'
        AND result_hash IS NOT NULL AND available_at<=? AND job_id>? ORDER BY job_id LIMIT 1'''
    row = await engine.first(query, now, active['evaluation_cursor'])
    row = row or await engine.first(query, now, '')
    if not row:
        return {'status': 'IDLE'}
    claim = await engine.run(f'''UPDATE cloud_research_automatic SET evaluation_cursor=?,
        evaluation_checked_at={PUBLICATION_CLOCK} WHERE slot=1 AND bundle_id=?
        AND evaluation_cursor=? AND evaluation_checked_at IS ?''', row['job_id'], active['bundle_id'],
        active['evaluation_cursor'], active['evaluation_checked_at'])
    if claim['meta']['changes'] != 1:
        return {'status': 'BUSY'}
    evaluated = await engine.evaluate(row['job_id'], now)
    return {'status': 'EVALUATED', 'job_id': row['job_id'], 'record': evaluated['record']}
