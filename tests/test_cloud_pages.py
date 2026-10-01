"""Synthetic saved-page adapter checks; no provider or Cloudflare requests."""
import asyncio
import json
import pytest
from hr_platform import fixtures as f
from hr_platform.cloud_pages import CloudPages
from hr_platform.common import canonical, stamp
from hr_platform.cloud_normalization import normalize_next
from test_cloud_history import cloud as cloud_fixture
from test_race_state import page, receipt
from test_official_payout import page as payout_page, receipt as payout_receipt

cloud = cloud_fixture


def seed(c, n, minute, kind='state', raw=None):
    raw = raw or (page('14:02現在', '') if kind == 'state' else payout_page())
    r = (receipt if kind == 'state' else payout_receipt)(raw, minute)
    event = c.seed(n, minute, raw, f'NAR_PAGE_{kind.upper()}', f'nar-daily-{kind}')
    c.db.conn.execute('UPDATE raw_observations SET received_at=?,raw_saved_at=? WHERE observation_id=?',
                      (stamp(r['collector_received_at']), stamp(r['raw_saved_at']), event))
    c.db.conn.execute('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)',
                      (event, n, kind, r['url'], f.RACE, stamp(f.at(-10))))
    c.db.conn.commit()
    c.bucket.objects[f'manifests/{event}.json'] = canonical({
        'event_id': event, 'url': r['url'], 'race_id': f.RACE, 'raw_sha256': r['sha256'],
        'raw_bytes': r['bytes'], 'http_status': r['status'], **{k: r[k] for k in (
            'fetch_started_at', 'headers_received_at', 'collector_received_at', 'raw_saved_at')}})
    return event


def test_state_reobservations_replay_reparse_and_no_backdating(cloud, monkeypatch):
    from hr_platform import cloud_pages as module
    async def run():
        h = CloudPages(cloud.bucket, cloud.db, cloud.h.clock)
        first = seed(cloud, 1, 0)
        one = await h.normalize(first)
        assert await h.normalize(first) == one
        old = await h.asof('state', f.RACE, f.at(5))
        assert old['evidence']['source_updated_at'] is None
        assert old['evidence']['pre_race_evidence'] is None
        assert not (await h.asof('state', f.RACE, f.at(4)))['evidence']
        cloud.clock[0] = f.at(6)
        second = seed(cloud, 2, 2)
        await h.normalize(second)
        assert (await h.asof('state', f.RACE, f.at(6)))['evidence']['observation_id'] == second
        assert await h.asof('state', f.RACE, f.at(5)) == old
        monkeypatch.setitem(module.VERSIONS, 'state', 'synthetic-reparse')
        cloud.clock[0] = f.at(7)
        await h.normalize(first)
        assert (await h.asof('state', f.RACE, f.at(7)))['evidence']['observation_id'] == second
        assert await h.asof('state', f.RACE, f.at(5)) == old
        history = await h.history('state', f.RACE, f.at(7), limit=2)
        assert len(history['history']) == 2 and history['next_cursor']
        assert len((await h.history('state', f.RACE, f.at(7), cursor=history['next_cursor']))['history']) == 1
        assert cloud.db.conn.execute('SELECT count(*) FROM raw_observations').fetchone()[0] == 2
    asyncio.run(run())


def test_quarantine_does_not_resurrect_older_good_state_and_results_stay_separate(cloud):
    async def run():
        h = CloudPages(cloud.bucket, cloud.db, cloud.h.clock)
        await h.normalize(seed(cloud, 1, 0))
        await h.normalize(seed(cloud, 2, 2, raw=b'<html>SYNTHETIC malformed</html>'))
        assert (await h.asof('state', f.RACE, f.at(5)))['evidence']['status'] == 'QUARANTINED'
        result = await h.normalize(seed(cloud, 3, 3, 'payout'))
        assert result['available_at']
        payout = (await h.asof('payout', f.RACE, f.at(5)))['evidence']
        assert payout['status'] == 'PAYOUT_QUALIFIED' and payout['final']
        assert sum('refund_per_100' in r for r in payout['tickets']) == 6
        assert not payout['paper_eligible']
        assert (await h.asof('state', f.RACE, f.at(5)))['evidence']['status'] == 'QUARANTINED'
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('url', 'https://invalid.example/'), ('http_status', 304),
                                         ('raw_bytes', 0), ('race_id', '20000101:SYNTHETIC:2')])
def test_invalid_receipt_cannot_publish(cloud, field, value):
    async def run():
        event = seed(cloud, 1, 0)
        key = f'manifests/{event}.json'
        m = json.loads(cloud.bucket.objects[key])
        m[field] = value
        cloud.bucket.objects[key] = canonical(m)
        with pytest.raises(ValueError):
            await CloudPages(cloud.bucket, cloud.db, cloud.h.clock).normalize(event)
        assert cloud.db.conn.execute('SELECT count(*) FROM page_parses').fetchone()[0] == 0
    asyncio.run(run())


def test_cron_publishes_page_and_recovers_failed_publication_at_repair_time(cloud):
    async def run():
        event = seed(cloud, 1, 0)
        h = CloudPages(cloud.bucket, cloud.db, cloud.h.clock)
        cloud.db.fail = 'UPDATE page_parses'
        with pytest.raises(RuntimeError):
            await h.normalize(event)
        assert not (await h.asof('state', f.RACE, f.at(5)))['evidence']
        cloud.db.fail = None
        cloud.clock[0] = f.at(6)
        assert (await normalize_next(cloud.bucket, cloud.db, None, 60, cloud.h.clock))['status'] == 'PARSED'
        assert not (await h.asof('state', f.RACE, f.at(5)))['evidence']
        assert (await h.asof('state', f.RACE, f.at(6)))['evidence']['available_at'] == stamp(f.at(6))
        assert (await normalize_next(cloud.bucket, cloud.db, None, 60, cloud.h.clock))['status'] == 'IDLE'
    asyncio.run(run())
