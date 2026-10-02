"""Synthetic bindings; verifies prospective selection and recovery, not live collection."""
import asyncio
import json
from pathlib import Path
import pytest
from hr_platform import fixtures as f
from hr_platform.cloud_paper_schedule import delayed_payout_packet, packet, schedule_day
from hr_platform.common import instant, stamp
from test_cloud_history import cloud as cloud_fixture
from test_cloud_paper import engine
from test_realdata import race_archive

cloud = cloud_fixture


def policies():
    schedule = json.loads(Path('configs/cloud-paper-schedule.json').read_text())
    schedule['venue_codes'] = {'SYNTHETIC': '19'}
    collection = json.loads(Path('configs/cloud-collection.json').read_text())
    collection['interval_seconds'] = json.loads(Path('configs/collection.json').read_text())['interval_seconds']
    return schedule, collection


class Collector:
    def __init__(self, p, status='REGISTERED'):
        self.p, self.status, self.calls = p, status, []

    async def scheduleEvidenceBatch(self, payload):
        payload = json.loads(payload)
        requests = payload['entries']
        self.calls.append(requests)
        if self.status != 'REGISTERED':
            return json.dumps({'status': self.status})
        for r in requests:
            await self.p.run('INSERT OR IGNORE INTO page_capture_plans VALUES(?,?,?,?,?,?,?,?)',
                f"nar-daily-{r['kind']}:{r['at']}", r['at'], r['kind'], r['url'], r['race_id'], await self.p.now(),
                policies()[0]['version'], payload['revision'])
            await self.p.run('UPDATE page_capture_plans SET packet_revision=? WHERE event_id=? AND packet_revision<?',
                            payload['revision'], f"nar-daily-{r['kind']}:{r['at']}", payload['revision'])
        return json.dumps({'status': 'REGISTERED'})


async def observed(p, c):
    c.clock[0] = f.at(-15)
    await p.races.normalize(c.seed(10, -20, race_archive(), 'NAR_RACE_BUNDLE', 'nar-daily-race'))


def test_future_enrollment_registers_one_packet_without_odds_or_results(cloud, config):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        policy, collection = policies()
        result = await schedule_day(p, collector, config, policy, collection)
        assert result['status'] == 'ENROLLED'
        plan = await p.plan(result['plan_id'])
        assert plan['asof_at'] == stamp(f.at(4)) and plan['registered_at'] < plan['asof_at']
        assert [r['kind'] for r in collector.calls[0]] == ['state', 'race', 'payout']
        assert len(await p.all('SELECT * FROM page_capture_plans')) == 3
        assert (await schedule_day(p, collector, config, policy, collection))['status'] == 'NO_FUTURE_SLOT'
        assert len(collector.calls) == 1
        assert not (await p.history(plan['id'], plan['registered_at']))['decisions']
        assert len(await p.all('SELECT * FROM cloud_paper_plans')) == 1
    asyncio.run(run())


@pytest.mark.parametrize('condition', ['capacity', 'stopped', 'late', 'other_packet', 'post_cutoff_packet'])
def test_ineligible_slots_do_not_enroll_or_fetch(cloud, config, condition):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p, 'INPUT_OR_CAPACITY_ERROR' if condition == 'capacity' else 'REGISTERED')
        if condition == 'stopped':
            await p.run("UPDATE source_control SET blocked=1 WHERE source='nar-daily-odds'")
        elif condition == 'late':
            cloud.clock[0] = f.at(0)
        elif condition in {'other_packet', 'post_cutoff_packet'}:
            await p.run('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)',
                'another-event', int(instant(f.at(5 if condition == 'post_cutoff_packet' else 0)).timestamp()*1000),
                'payout', 'SYNTHETIC', '20000101:OTHER:1', stamp(f.at(-15)))
        result = await schedule_day(p, collector, config, *policies())
        assert result['status'] in {'SOURCE_STOPPED', 'NO_FUTURE_SLOT', 'COLLECTION_PLAN_PENDING'}
        assert not await p.all('SELECT * FROM cloud_paper_plans')
        assert len(collector.calls) == (1 if condition == 'capacity' else 0)
    asyncio.run(run())


def test_registration_failure_recovers_existing_packet_once_without_backdating(cloud, config):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        original = p.register
        async def failed(*args):
            raise RuntimeError('synthetic enrollment failure after packet reservation')
        p.register = failed
        with pytest.raises(RuntimeError):
            await schedule_day(p, collector, config, *policies())
        assert len(await p.all('SELECT * FROM page_capture_plans')) == 3
        p.register = original
        cloud.clock[0] = f.at(-5)
        result = await schedule_day(p, collector, config, *policies())
        assert result['status'] == 'ENROLLED'
        assert (await p.plan(result['plan_id']))['registered_at'] == stamp(f.at(-5))
        assert len(await p.all('SELECT * FROM page_capture_plans')) == 3
        assert len(await p.all('SELECT * FROM cloud_paper_plans')) == 1
    asyncio.run(run())


@pytest.mark.parametrize(('spacing', 'decided', 'expected'), [
    (6, False, 'NO_FUTURE_SLOT'), (7, False, 'NO_FUTURE_SLOT'),
    (-26, False, 'NO_FUTURE_SLOT'), (8, False, 'ENROLLED'), (6, True, 'ENROLLED'),
])
def test_new_packet_preserves_existing_races_last_odds_slot(cloud, config, spacing, decided, expected):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        policy, collection = policies()
        other = '20000101:SYNTHETIC:12'
        await p.run('''INSERT INTO cloud_paper_plans
            (plan_id,experiment,race_id,day,asof_at,plan_body,registered_at,decisions)
            VALUES(?,?,?,?,?,?,?,?)''', 'other', 'synthetic', other, '20000101',
            stamp(f.at(4-spacing)), json.dumps({'revision_id': 'other', 'config': config}),
            stamp(f.at(-15)), '[]' if decided else None)
        for r in packet(other, f.at(14-spacing), f.at(4-spacing), policy, collection):
            await p.run('INSERT INTO page_capture_plans VALUES(?,?,?,?,?,?,?,?)',
                f"nar-daily-{r['kind']}:{r['at']}", r['at'], r['kind'], r['url'], other,
                stamp(f.at(-15)), policy['version'], 'synthetic-revision')
        before = await p.all('SELECT * FROM page_capture_plans WHERE race_id=?', other)
        collector = Collector(p)
        # Existing pages do not intersect the new race's window. Its state page
        # still steals the earlier race's final odds via nextPage's lookahead.
        assert (await schedule_day(p, collector, config, policy, collection))['status'] == expected
        assert len(collector.calls) == (expected == 'ENROLLED')
        assert await p.all('SELECT * FROM page_capture_plans WHERE race_id=?', other) == before
    asyncio.run(run())


def test_compact_schedule_read_respects_availability_and_preserves_past_view(cloud):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        assert not (await p.races.schedules('20000101', f.at(-16)))['races']
        old = await p.races.schedules('20000101', f.at(-15))
        assert set(old['races']) == {f.RACE}
        original = cloud.bucket.get
        calls = []
        async def read(key):
            calls.append(key)
            return await original(key)
        cloud.bucket.get = read
        assert (await p.races.schedules('20000101', f.at(-15))) == old
        assert len(calls) == 1
        cloud.clock[0] = f.at(-10)
        event = cloud.seed(11, -11, race_archive(), 'NAR_RACE_BUNDLE', 'nar-daily-race')
        await p.races.normalize(event)
        assert (await p.races.schedules('20000101', f.at(-15))) == old
    asyncio.run(run())


def test_schedule_revision_enrolls_new_cutoff_and_manual_slots_still_conflict(cloud, config):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        old = await schedule_day(p, collector, config, *policies())
        previous = await p.plan(old['plan_id'])
        cloud.clock[0] = f.at(-14, 2)
        await p.races.normalize(cloud.seed(11, -14, race_archive(start='1416'), 'NAR_RACE_BUNDLE', 'nar-daily-race'))
        newer = await schedule_day(p, collector, config, *policies())
        revised = await p.plan(newer['plan_id'])
        assert revised['id'] == previous['id'] and revised['asof_at'] == stamp(f.at(6))
        assert revised['supersedes'] == previous['revision_id']
        assert await p.plan(previous['id'], f.at(-15)) == previous
        assert len(collector.calls) == 2
        cloud.clock[0] = f.at(-13, 2)
        await p.races.normalize(cloud.seed(12, -13, race_archive(start='1418'), 'NAR_RACE_BUNDLE', 'nar-daily-race'))
        await p.run('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)',
                    'manual-own-race', round(instant(f.at(3)).timestamp()*1000), 'state', 'SYNTHETIC', f.RACE, stamp(f.at(-13)))
        assert (await schedule_day(p, collector, config, *policies()))['status'] == 'NO_FUTURE_SLOT'
        assert len(collector.calls) == 2
    asyncio.run(run())


def test_same_schedule_new_observation_updates_reservation_version_without_new_decision(cloud, config):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        result = await schedule_day(p, collector, config, *policies())
        plan = await p.plan(result['plan_id'])
        cloud.clock[0] = f.at(-14, 2)
        parsed = await p.races.normalize(cloud.seed(11, -14, race_archive(), 'NAR_RACE_BUNDLE', 'nar-daily-race'))
        assert (await schedule_day(p, collector, config, *policies()))['status'] == 'ENROLLED'
        assert len(collector.calls) == 2
        rows = await p.all('SELECT packet_revision FROM page_capture_plans')
        assert len(rows) == 3 and all(r['packet_revision'].endswith(parsed['parse_id']) for r in rows)
        assert await p.plan(plan['id']) == plan
        assert (await schedule_day(p, collector, config, *policies()))['status'] == 'NO_FUTURE_SLOT'
        assert len(collector.calls) == 2 and not (await p.history(plan['id'], await p.now()))['decisions']
    asyncio.run(run())


@pytest.mark.parametrize('condition', ['future', 'late', 'settled', 'conflict'])
def test_delay_after_decision_moves_only_payout_and_never_reprices(cloud, config, condition):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        result = await schedule_day(p, collector, config, *policies())
        original = await p.plan(result['plan_id'])
        cloud.clock[0] = f.at(4, 10)
        before = await p.decide(original['id'])
        cloud.clock[0] = f.at(65 if condition == 'late' else 5)
        await p.races.normalize(cloud.seed(11, 4, race_archive(start='1444', finished=condition == 'late'),
                                          'NAR_RACE_BUNDLE', 'nar-daily-race'))
        if condition == 'settled':
            await p.run('INSERT INTO cloud_paper_settlements VALUES(?,?,?,?)',
                        original['id'], 'synthetic-evidence', 'synthetic-body', stamp(f.at(5)))
        if condition == 'conflict':
            await p.run('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)',
                        'another-payout', round(instant(f.at(60)).timestamp()*1000), 'payout', 'SYNTHETIC',
                        '20000101:OTHER:1', stamp(f.at(5)))
        changed = await schedule_day(p, collector, config, *policies())
        if condition in {'settled', 'conflict'}:
            assert changed['status'] == 'NO_FUTURE_SLOT'
            assert len(collector.calls) == 1
        else:
            assert changed['status'] == 'PAYOUT_RESCHEDULED'
            assert collector.calls[1][:2] == collector.calls[0][:2]
            expected = 71 if condition == 'late' else 60
            assert collector.calls[1][-1]['at'] == round(instant(f.at(expected)).timestamp()*1000)
            assert (await schedule_day(p, collector, config, *policies()))['status'] == 'NO_FUTURE_SLOT'
            assert len(collector.calls) == 2
        assert await p.plan(original['id']) == original
        assert await p.decide(original['id']) == before
        assert len(await p.all('SELECT * FROM cloud_paper_plan_revisions')) == 1
    asyncio.run(run())


@pytest.mark.parametrize('other_cutoff', [61, 60, 59, 58])
def test_delayed_payout_cannot_consume_another_plans_last_odds_slot(cloud, config, other_cutoff):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        await schedule_day(p, Collector(p), config, *policies())
        captures = await p.all('SELECT *,NULL AS capture_status FROM page_capture_plans')
        policy, collection = policies()
        # Payout at 15:00 also preempts odds just before a 14:58/14:59 cutoff:
        # the collector looks one interval ahead when choosing a page.
        other = {'race_id': '20000101:OTHER:1', 'asof_at': f.at(other_cutoff), 'decisions': None}
        metadata = {'scheduled_start_at': f.at(44)}
        assert delayed_payout_packet(f.RACE, metadata, captures, [other], f.at(5), policy, collection) is None
        assert delayed_payout_packet(f.RACE, metadata, captures, [], f.at(5), policy, collection)
    asyncio.run(run())


@pytest.mark.parametrize('condition', ['missed', 'pending', 'stored', 'failed', 'stopped', 'conflict'])
def test_missed_payout_reserves_new_observation_without_rewriting_old_plan(cloud, config, condition):
    async def run():
        p = engine(cloud)
        await observed(p, cloud)
        collector = Collector(p)
        result = await schedule_day(p, collector, config, *policies())
        original = await p.plan(result['plan_id'])
        cloud.clock[0] = f.at(4, 10)
        decisions = await p.decide(original['id'])
        payout = (await p.all("SELECT * FROM page_capture_plans WHERE kind='payout'"))[0]
        status = {'pending': None, 'stored': 'RAW_STORED', 'failed': 'FAILED'}.get(condition, 'MISSED_WINDOW')
        if status:
            await p.run('INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,?)',
                        payout['event_id'], stamp(f.at(30)), status)
        cloud.clock[0] = f.at(40)
        if condition == 'stopped':
            await p.run("UPDATE source_control SET blocked=1 WHERE source='nar-daily-odds'")
        if condition == 'conflict':
            # A new payout at :46 must not consume the other race's last odds slot.
            await p.run('''INSERT INTO cloud_paper_plans(plan_id,experiment,race_id,day,asof_at,plan_body,registered_at)
                SELECT ?,experiment,?,day,?,json_set(plan_body,'$.revision_id','other-revision'),registered_at
                FROM cloud_paper_plans LIMIT 1''',
                        'other-plan', '20000101:OTHER:1', stamp(f.at(47)))
        outcome = await schedule_day(p, collector, config, *policies())
        if condition == 'missed':
            assert outcome['status'] == 'PAYOUT_RESCHEDULED'
            assert collector.calls[1][:2] == collector.calls[0][:2]
            assert collector.calls[1][-1]['at'] == round(instant(f.at(46)).timestamp() * 1000)
            assert (await schedule_day(p, collector, config, *policies()))['status'] == 'NO_FUTURE_SLOT'
            assert len(collector.calls) == 2
        else:
            assert outcome['status'] == ('SOURCE_STOPPED' if condition == 'stopped' else 'NO_FUTURE_SLOT')
            assert len(collector.calls) == 1
        assert await p.first('SELECT * FROM page_capture_plans WHERE event_id=?', payout['event_id']) == payout
        saved = await p.first('SELECT status FROM captures WHERE event_id=?', payout['event_id'])
        assert (saved['status'] if saved else None) == status
        assert await p.plan(original['id']) == original
        assert await p.decide(original['id']) == decisions
    asyncio.run(run())
