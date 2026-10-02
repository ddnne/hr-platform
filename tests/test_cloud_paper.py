"""Cloud bindings are synthetic SQLite/R2; arithmetic uses the existing model."""
import asyncio
import json
from pathlib import Path
import pytest
from hr_platform import fixtures as f
from hr_platform.cloud_paper import CloudPaper
from hr_platform.common import stamp
from test_cloud_history import cloud as cloud_fixture
from test_cloud_pages import seed as seed_page
from test_realdata import race_archive
from test_race_state import page
from test_official_payout import page as payout_page

cloud = cloud_fixture


def engine(c):
    return CloudPaper(c.bucket, c.db, c.h.clock, paper_policy=json.loads(Path('configs/cloud-paper.json').read_text()))


async def inputs(c, p, config):
    c.clock[0] = f.at(0)
    event = c.seed(10, -2, race_archive(), 'NAR_RACE_BUNDLE', 'nar-daily-race')
    await p.races.normalize(event)
    plan = await p.enroll(f.RACE, config)
    assert plan['asof_at'] == stamp(f.at(4))
    c.clock[0] = f.at(3)
    event = c.seed(11, 2, race_archive(), 'NAR_RACE_BUNDLE', 'nar-daily-race')
    await p.races.normalize(event)
    event = seed_page(c, 12, 2, raw=page('14:02現在', '').replace(b'14:10', b'14:14'))
    await p.pages.normalize(event)
    await p.odds.normalize(c.seed(13, 2))
    return plan


def test_automatic_three_models_then_official_refunds_and_asof_replay(cloud, config):
    async def run():
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        assert (await p.tick())['status'] == 'IDLE'
        assert (await p.decide(plan['id']))['status'] == 'NOT_DUE'
        cloud.clock[0] = f.at(4, 10)
        decisions = await p.tick()
        assert len(decisions['decisions']) == 3
        assert all(d['status'] == 'PAPER_BET' and d['stake_yen'] == 100 for d in decisions['decisions'])
        assert len({d['decision_at'] for d in decisions['decisions']}) == 1
        assert not (await p.history(plan['id'], f.at(4)))['decisions']
        original = await p.history(plan['id'], f.at(4, 10))
        assert not original['settlements']
        cloud.clock[0] = f.at(21)
        event = seed_page(cloud, 14, 20, 'payout', payout_page('除外'))
        await p.pages.normalize(event)
        settled = await p.tick()
        assert settled['status'] == 'RECORDED'
        assert all(s['refund_yen'] == 100 and s['payout_yen'] == 0 for s in settled['settlements'])
        assert await p.settle(plan['id']) == settled
        assert await p.decide(plan['id']) == decisions
        assert await p.history(plan['id'], f.at(4, 10)) == original
        assert len((await p.history(plan['id'], f.at(21)))['settlements']) == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['late_model', 'raise', 'late_storage', 'missing_state'])
def test_failures_consume_one_no_bet_without_repricing(cloud, config, failure):
    async def run():
        from hr_platform.cloud_model import execute
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        if failure == 'missing_state':
            cloud.db.conn.execute('DELETE FROM page_parses')
            cloud.db.conn.commit()
        cloud.clock[0] = f.at(4, 10)
        def analyzer(payload):
            if failure == 'raise':
                raise RuntimeError('synthetic solver failure')
            result = execute(payload)
            if failure == 'late_model':
                cloud.clock[0] = f.at(7)
            return result
        original_save = p.save_body
        async def delayed_save(data):
            result = await original_save(data)
            if failure == 'late_storage':
                cloud.clock[0] = f.at(7)
            return result
        p.save_body = delayed_save
        decisions = await p.decide(plan['id'], analyzer)
        assert all(d['status'] == 'NO_BET' and d['stake_yen'] == 0 for d in decisions['decisions'])
        assert await p.decide(plan['id'], lambda _: pytest.fail('must not reprice')) == decisions
    asyncio.run(run())


def test_frozen_config_and_atomic_daily_budget(cloud, config):
    async def run():
        p = engine(cloud)
        config['daily_stake_limit_yen_per_model'] = 50
        plan = await inputs(cloud, p, config)
        with pytest.raises(ValueError, match='EXPERIMENT_CONFIG_CHANGED'):
            await p.enroll(f.RACE, {**config, 'lambda': 0.2})
        cloud.clock[0] = f.at(4, 10)
        result = await p.tick()
        assert all(d['reason'] == 'DAILY_LIMIT' and d['stake_yen'] == 0 for d in result['decisions'])
        assert await p.enroll(f.RACE, config) == plan
    asyncio.run(run())


def test_unqualified_payout_stays_pending_and_claim_recovery_cannot_bet_late(cloud, config):
    async def run():
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        cloud.db.conn.execute('UPDATE cloud_paper_plans SET owner=?,lease_until=?', ('lost', stamp(f.at(7))))
        cloud.db.conn.commit()
        cloud.clock[0] = f.at(4, 10)
        assert (await p.decide(plan['id']))['status'] == 'BUSY'
        cloud.clock[0] = f.at(8)
        result = await p.tick()
        assert all(d['reason'] == 'DECISION_TOO_LATE' for d in result['decisions'])
        assert (await p.settle(plan['id']))['status'] == 'PAYOUT_PENDING'
    asyncio.run(run())


@pytest.mark.parametrize('start,cutoff', [(18, 8), (12, 2)])
def test_schedule_revision_preserves_history_and_never_backfills(cloud, config, start, cutoff):
    async def run():
        p = engine(cloud)
        original = await inputs(cloud, p, config)
        metadata = p.metadata
        async def changed(race, at):
            result = await metadata(race, at)
            result['evidence']['metadata']['scheduled_start_at'] = f.at(start)
            result['evidence']['available_at'] = stamp(f.at(3))
            return result
        p.metadata = changed
        result = await p.tick()
        revised = await p.plan(original['id'])
        assert revised['asof_at'] == stamp(f.at(cutoff))
        assert revised['supersedes'] == original['revision_id']
        assert await p.plan(original['id'], f.at(0)) == original
        if cutoff > 3:
            assert result['status'] == 'IDLE'
            cloud.clock[0] = f.at(cutoff, 10)
            result = await p.tick()
        else:
            assert all(d['reason'] == 'PROSPECTIVE_PLAN_REQUIRED' for d in result['decisions'])
        assert all(d['asof_at'] == stamp(f.at(cutoff)) for d in result['decisions'])
        p.metadata = metadata
        assert await p.enroll(f.RACE, config) == revised
        assert await p.decide(original['id']) == result
        assert len(await p.all('SELECT * FROM cloud_paper_plan_revisions')) == 2
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['missing_body', 'details', 'expired_and_missing'])
def test_storage_failure_still_records_fixed_no_bet(cloud, config, failure):
    async def run():
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        cloud.clock[0] = f.at(8) if failure == 'expired_and_missing' else f.at(4, 10)
        async def broken(*_):
            raise ValueError('synthetic missing object')
        if failure != 'details':
            cloud.bucket.objects.clear()
        p.save_body = broken
        result = await p.tick()
        reason = {'missing_body': 'INPUT_UNAVAILABLE', 'details': 'DETAILS_UNAVAILABLE',
                  'expired_and_missing': 'DECISION_TOO_LATE'}[failure]
        assert all(d['reason'] == reason and d['stake_yen'] == 0 for d in result['decisions'])
        assert result['details_hash'] is None
        assert await p.decide(plan['id']) == result
    asyncio.run(run())


def test_unusable_odds_are_data_missing_not_solver_failure(cloud, config):
    async def run():
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        from hr_platform.cloud_model import execute
        def analyzer(payload):
            data = json.loads(payload)
            next(iter(data['markets']['exacta']['quotes'].values()))['odds'] = 0.0
            return execute(json.dumps(data))
        cloud.clock[0] = f.at(4, 10)
        result = await p.decide(plan['id'], analyzer)
        assert all(d['reason'] == 'DATA_MISSING' for d in result['decisions'])
    asyncio.run(run())


def test_observed_d1_clock_can_lead_worker_without_allowing_future_inputs(cloud, config):
    async def run():
        from datetime import timedelta
        from hr_platform.common import instant
        cloud.db.clock = lambda: (instant(cloud.clock[0]) + timedelta(seconds=1)).isoformat()
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        cloud.clock[0] = f.at(4, 10)
        result = await p.tick()
        assert all(d['status'] == 'PAPER_BET' for d in result['decisions'])
        assert result['decision_at'] == stamp(f.at(4, 11))
        with pytest.raises(ValueError, match='ASOF_IN_FUTURE'):
            await p.odds.asof(f.RACE, ['win'], f.at(4, 12))
        assert not (await p.history(plan['id'], f.at(4, 10)))['decisions']
        cloud.clock[0] = f.at(4, 12)
        assert (await p.history(plan['id'], f.at(4, 11)))['decisions'] == result['decisions']
    asyncio.run(run())


@pytest.mark.parametrize('after_wait', ['on_time', 'late', 'rescheduled'])
def test_cron_before_the_minute_waits_for_fixed_decision(cloud, config, monkeypatch, after_wait):
    async def run():
        config['max_decision_delay_seconds'] = 30
        p = engine(cloud)
        plan = await inputs(cloud, p, config)
        cloud.clock[0] = f.at(3, 39)
        calls = []
        metadata = p.metadata
        async def wait(delay):
            calls.append(delay)
            assert (await p.decision(plan['id']))['decisions'] is None
            cloud.clock[0] = f.at(4, 31) if after_wait == 'late' else f.at(4)
            if after_wait == 'rescheduled':
                async def changed(race, at):
                    result = await metadata(race, at)
                    result['evidence']['metadata']['scheduled_start_at'] = f.at(18)
                    result['evidence']['available_at'] = stamp(f.at(3, 50))
                    return result
                p.metadata = changed
        monkeypatch.setattr('hr_platform.cloud_paper.asyncio.sleep', wait)
        # Direct RPC/default tick keeps its non-waiting behavior.
        assert (await p.tick())['status'] == 'IDLE'
        assert not calls
        result = await p.tick(wait_for_due=True)
        assert calls == [21]
        assert not (await p.history(plan['id'], f.at(3, 59)))['decisions']
        if after_wait == 'rescheduled':
            assert result['status'] == 'NOT_DUE'
            assert (await p.plan(plan['id']))['asof_at'] == stamp(f.at(8))
            assert (await p.decision(plan['id']))['decisions'] is None
        else:
            assert all(d['asof_at'] == plan['asof_at'] for d in result['decisions'])
            assert all(d['status'] == ('NO_BET' if after_wait == 'late' else 'PAPER_BET')
                       for d in result['decisions'])
            if after_wait == 'late':
                assert all(d['reason'] == 'DECISION_TOO_LATE' for d in result['decisions'])
            assert await p.tick(wait_for_due=True) == {'status': 'PAYOUT_PENDING'}
            assert calls == [21]
    asyncio.run(run())
