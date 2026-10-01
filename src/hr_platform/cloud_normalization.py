"""Parse one saved observation per invocation. This module never contacts providers."""
from datetime import timedelta
from uuid import uuid4
from .cloud_history import CloudHistory, VERSION as ODDS_VERSION
from .cloud_race_files import CloudRaceFiles, VERSION as RACE_VERSION
from .cloud_pages import CloudPages, VERSIONS as PAGE_VERSIONS
from .common import instant, stamp


async def normalize_next(bucket, db, storage_policy, lease_seconds, clock=None):
    kwargs = {'storage_policy': storage_policy}
    if clock is not None:
        kwargs['clock'] = clock
    history = CloudHistory(bucket, db, **kwargs)
    now = stamp(history.clock())
    if type(lease_seconds) is not int or lease_seconds < 60:
        raise ValueError('LEASE_SECONDS')
    item = await history.first('''SELECT o.observation_id,o.dataset_kind,
        CASE o.dataset_kind WHEN 'NAR_RACE_BUNDLE' THEN ? WHEN 'NAR_PAGE_STATE' THEN ?
          WHEN 'NAR_PAGE_PAYOUT' THEN ? ELSE ? END AS version
        FROM raw_observations o
        LEFT JOIN normalization_jobs j ON j.observation_id=o.observation_id AND j.version=
          CASE o.dataset_kind WHEN 'NAR_RACE_BUNDLE' THEN ? WHEN 'NAR_PAGE_STATE' THEN ?
          WHEN 'NAR_PAGE_PAYOUT' THEN ? ELSE ? END
        WHERE o.dataset_kind IN ('DAILY_SNAPSHOT','NAR_RACE_BUNDLE','NAR_PAGE_STATE','NAR_PAGE_PAYOUT')
          AND (j.observation_id IS NULL OR (j.status!='DONE' AND j.lease_until<=?))
          AND NOT EXISTS (SELECT 1 FROM odds_parses p WHERE p.observation_id=o.observation_id
            AND p.version=? AND p.encoding='utf-8-sig' AND p.status IN ('OK','ERROR'))
          AND NOT EXISTS (SELECT 1 FROM race_file_parses p WHERE p.observation_id=o.observation_id
            AND p.version=? AND p.encoding='utf-8-sig' AND p.status IN ('OK','ERROR'))
          AND NOT EXISTS (SELECT 1 FROM page_parses p WHERE p.observation_id=o.observation_id
            AND p.version IN (?,?) AND p.available_at IS NOT NULL)
        ORDER BY o.received_at DESC,
          CASE WHEN o.dataset_kind='NAR_RACE_BUNDLE' THEN 0 ELSE 1 END,
          o.observation_id DESC LIMIT 1''',
        RACE_VERSION, PAGE_VERSIONS['state'], PAGE_VERSIONS['payout'], ODDS_VERSION,
        RACE_VERSION, PAGE_VERSIONS['state'], PAGE_VERSIONS['payout'], ODDS_VERSION, now, ODDS_VERSION, RACE_VERSION,
        PAGE_VERSIONS['state'], PAGE_VERSIONS['payout'])
    if not item:
        return {'status': 'IDLE'}
    owner = str(uuid4())
    until = stamp((instant(now) + timedelta(seconds=lease_seconds)).isoformat())
    key = (item['observation_id'], item['version'])
    await history.run('''INSERT INTO normalization_jobs VALUES(?,?,?,?,'RUNNING')
        ON CONFLICT(observation_id,version) DO UPDATE SET owner=excluded.owner,
        lease_until=excluded.lease_until,status='RUNNING'
        WHERE normalization_jobs.status!='DONE' AND normalization_jobs.lease_until<=?''',
        *key, owner, until, now)
    lease = await history.first('SELECT owner FROM normalization_jobs WHERE observation_id=? AND version=?', *key)
    if lease['owner'] != owner:
        return {'status': 'BUSY'}
    parser = (CloudPages(bucket, db, **kwargs) if item['dataset_kind'].startswith('NAR_PAGE_')
              else CloudRaceFiles(bucket, db, **kwargs) if item['dataset_kind'] == 'NAR_RACE_BUNDLE' else history)
    result = await parser.normalize(item['observation_id'])
    await history.run("UPDATE normalization_jobs SET status='DONE' WHERE observation_id=? AND version=? AND owner=?",
                      *key, owner)
    return {'status': 'PARSED' if result.get('status') == 'OK' or result.get('available_at') else 'PARSE_ERROR'}
