"""Prospective shared schedules and fair evaluation; all bindings are synthetic."""
import asyncio
from copy import deepcopy

import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical, stamp
from test_cloud_history import cloud as cloud_fixture
from test_cloud_research import engine
from test_cloud_research_evaluation import completed
from test_realdata import race_archive
from test_research_suite import configs

cloud = cloud_fixture


async def active(c, config):
    h = engine(c)
    b = await h.register('SYNTHETIC-AUTOMATIC', config, configs())
    await h.activate(b['bundle_id'], '20000101')
    return h, b


async def schedule(c, h, number=90, start='1414'):
    event = c.seed(number, -2, race_archive(start=start), 'NAR_RACE_BUNDLE', 'nar-daily-race')
    await h.races.normalize(event)


def test_automatic_future_job_is_fixed_before_cutoff_and_never_runs_early(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h)
        before = await h.activate(b['bundle_id'], '20000101')
        planned = await h.schedule_registered()
        assert planned['status'] == 'SCHEDULED' and len(planned['job_ids']) == 1
        job = (await h.jobs(b['bundle_id']))[0]
        assert job['asof_at'] == stamp(f.at(4)) and job['registered_at'] < job['asof_at']
        original = deepcopy(job)
        assert await h.schedule_registered() == {'status': 'IDLE', 'job_ids': []}
        assert await h.activate(b['bundle_id'], '20000101') == before
        assert (await h.tick(lambda *_: pytest.fail('future cutoff must wait')))['status'] == 'IDLE'
        assert (await h.jobs(b['bundle_id']))[0] == original
        cloud.clock[0] = f.at(4)
        assert (await h.tick(lambda *_: pytest.fail('missing input cannot be priced')))['status'] == 'COMPLETE'
        result = (await h.result(job['job_id'], f.at(4)))['result']
        saved = await h.read_body(result['input_hash'])
        assert saved['enrollment']['known_at'] == stamp(f.at(0))
        assert not result['timing_qualified'] and not result['paper_eligible']
        assert len(result['output']['candidates']) == 91
        assert all(s['status'] == 'INPUT_EXCLUDED' for s in result['output']['candidates'].values())
        assert not await h.all('SELECT * FROM cloud_paper_plans')
    asyncio.run(run())


def test_changed_and_restored_future_start_keeps_one_logical_job_and_old_r2_plan(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h)
        await h.schedule_registered()
        first = (await h.jobs(b['bundle_id']))[0]
        old_plan = await h.read_body(first['schedule_hash'])
        cloud.clock[0] = f.at(1)
        await schedule(cloud, h, 91, '1416')
        await h.schedule_registered()
        rows = await h.jobs(b['bundle_id'])
        assert sum(j['status'] == 'QUEUED' for j in rows) == 1
        assert next(j for j in rows if j['job_id'] == first['job_id'])['status'] == 'SUPERSEDED'
        cloud.clock[0] = f.at(2)
        await schedule(cloud, h, 92, '1414')
        await h.schedule_registered()
        rows = await h.jobs(b['bundle_id'])
        restored = next(j for j in rows if j['status'] == 'QUEUED')
        assert restored['job_id'] == first['job_id'] and len(rows) == 2
        assert restored['schedule_hash'] != first['schedule_hash']
        assert (await h.read_body(restored['schedule_hash']))['supersedes_schedule_hash'] == first['schedule_hash']
        assert await h.read_body(first['schedule_hash']) == old_plan
        assert await h.schedule_registered() == {'status': 'IDLE', 'job_ids': []}
    asyncio.run(run())


def test_schedule_published_after_fixed_cutoff_cannot_replace_it(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h)
        await h.schedule_registered()
        first = (await h.jobs(b['bundle_id']))[0]
        cloud.clock[0] = f.at(5)
        await schedule(cloud, h, 91, '1416')
        assert (await h.schedule_registered())['status'] == 'IDLE'
        assert (await h.jobs(b['bundle_id']))[0] == first
        assert (await h.tick())['status'] == 'COMPLETE'
        result = (await h.result(first['job_id'], f.at(5)))['result']
        assert not result['timing_qualified']
        assert (await h.read_body(result['input_hash']))['enrollment']['scheduled_start_at'] == old_start()
    asyncio.run(run())


def old_start():
    return stamp(f.at(14))


@pytest.mark.parametrize('condition', ['future_date', 'outside_horizon', 'past_cutoff', 'wrong_engine'])
def test_unavailable_or_ineligible_automatic_schedules_do_not_create_paper_or_fetch(cloud, config, condition):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h, start='1614' if condition == 'outside_horizon' else '1414')
        if condition == 'future_date':
            await h.activate(b['bundle_id'], '20000102')
        elif condition == 'past_cutoff':
            cloud.clock[0] = f.at(5)
        elif condition == 'wrong_engine':
            h.engine_id = 'b' * 64
        assert (await h.schedule_registered())['status'] in {'IDLE', 'NOT_DUE', 'NO_ACTIVE_BUNDLE'}
        assert not await h.jobs(b['bundle_id'])
        assert not await h.all('SELECT * FROM cloud_paper_plans')
        assert len(await h.all('SELECT * FROM raw_observations')) == 1
    asyncio.run(run())


def test_automatic_evaluation_rotates_without_refit_and_old_job_fields_stay_fixed(cloud, config):
    async def run():
        h, job, result = await completed(cloud, config)
        await h.activate(job['bundle_id'], '20000101')
        other = await h.enqueue(job['bundle_id'], f.RACE, f.at(0))
        await h.tick(lambda *_: pytest.fail('past missing inputs cannot price'))
        before = await h.all('SELECT * FROM cloud_research_jobs ORDER BY job_id')
        first = await h.refresh_evaluations()
        assert first['status'] == 'EVALUATED'
        assert (await h.refresh_evaluations())['status'] == 'NOT_DUE'
        cloud.clock[0] = f.at(6)
        second = await h.refresh_evaluations()
        assert second['status'] == 'EVALUATED' and second['job_id'] != first['job_id']
        assert {first['job_id'], second['job_id']} == {job['job_id'], other['job_id']}
        cloud.clock[0] = f.at(7)
        repeat = await h.refresh_evaluations()
        assert repeat['job_id'] == first['job_id'] and repeat['record'] == first['record']
        assert await h.all('SELECT * FROM cloud_research_jobs ORDER BY job_id') == before
        assert canonical((await h.result(job['job_id'], f.at(7)))['result']) == canonical(result['result'])
        assert not await h.all('SELECT * FROM cloud_paper_settlements')
    asyncio.run(run())


def test_failed_evaluation_does_not_starve_next_completed_job(cloud, config):
    async def run():
        h, job, _ = await completed(cloud, config)
        await h.activate(job['bundle_id'], '20000101')
        await h.enqueue(job['bundle_id'], f.RACE, f.at(0))
        await h.tick()
        old = h.evaluate
        async def failed(*args, **kwargs):
            raise RuntimeError('SYNTHETIC_EVALUATION_FAILURE')
        h.evaluate = failed
        with pytest.raises(RuntimeError, match='SYNTHETIC_EVALUATION_FAILURE'):
            await h.refresh_evaluations()
        first = (await h.first('SELECT * FROM cloud_research_automatic'))['evaluation_cursor']
        cloud.clock[0] = f.at(6)
        h.evaluate = old
        assert (await h.refresh_evaluations())['job_id'] != first
    asyncio.run(run())


def test_d1_clock_ahead_does_not_fail_future_job(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h)
        await h.schedule_registered()
        before = (await h.jobs(b['bundle_id']))[0]
        cloud.clock[0] = f.at(3)
        cloud.db.clock = lambda: f.at(4)
        assert (await h.tick())['status'] == 'IDLE'
        assert (await h.jobs(b['bundle_id']))[0] == before
        cloud.clock[0] = f.at(4)
        assert (await h.tick())['status'] == 'COMPLETE'
    asyncio.run(run())


def test_expired_old_planner_cannot_rewind_new_schedule(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h, start='1418')
        read, resume = asyncio.Event(), asyncio.Event()
        original = h.races.schedules
        async def paused(*args):
            snapshot = await original(*args)
            read.set()
            await resume.wait()
            return snapshot
        h.races.schedules = paused
        old = asyncio.create_task(h.schedule_registered())
        await read.wait()
        newer = engine(cloud)
        assert (await newer.schedule_registered())['status'] == 'BUSY'
        cloud.clock[0] = f.at(4)  # Original owner's three-minute lease expired.
        await schedule(cloud, newer, 91, '1420')
        assert (await newer.schedule_registered())['status'] == 'SCHEDULED'
        before = await h.jobs(b['bundle_id'])
        resume.set()
        await old
        assert await h.jobs(b['bundle_id']) == before
        assert len(before) == 1 and before[0]['asof_at'] == stamp(f.at(10))
        assert before[0]['status'] == 'QUEUED'
    asyncio.run(run())


def test_changed_activation_before_claim_cannot_use_old_start_date(cloud, config):
    async def run():
        cloud.clock[0] = f.at(0)
        h, b = await active(cloud, config)
        await schedule(cloud, h)
        read, resume = asyncio.Event(), asyncio.Event()
        original = h.first
        async def paused(query, *args):
            row = await original(query, *args)
            if 'JOIN cloud_research_bundles b USING(bundle_id) WHERE slot=1' in query:
                read.set()
                await resume.wait()
            return row
        h.first = paused
        old = asyncio.create_task(h.schedule_registered())
        await read.wait()
        cloud.clock[0] = f.at(1)
        newer = engine(cloud)
        await newer.activate(b['bundle_id'], '20000102')
        resume.set()
        assert (await old)['status'] == 'BUSY'
        assert (await newer.schedule_registered())['status'] == 'NOT_DUE'
        assert not await h.jobs(b['bundle_id'])
    asyncio.run(run())
