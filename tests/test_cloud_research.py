"""Synthetic R2/D1 research queue. Actual Cloudflare timing is measured separately."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from hr_platform import fixtures as f
from hr_platform.cloud_research import CloudResearch
from hr_platform.common import identity, stamp
from test_cloud_history import cloud as cloud_fixture
from test_cloud_paper import engine as paper_engine, inputs
from test_research_suite import configs

cloud = cloud_fixture


def engine(c, **options):
    return CloudResearch(c.bucket, c.db, c.h.clock,
        research_policy=json.loads(Path('configs/cloud-research.json').read_text()), engine_id='a' * 64, **options)


@pytest.mark.parametrize('policy', [None, 'require_feasible', 'allow_inconsistent_shadow'])
def test_cloud_research_and_paper_share_reference_policy(cloud, config, monkeypatch, policy):
    original = f.markets
    def inconsistent(*args, **kwargs):
        markets = original(*args, **kwargs)
        markets['win']['quotes']['1']['odds'] *= 0.5
        return markets
    monkeypatch.setattr(f, 'markets', inconsistent)
    base = {**config, **({'reference_constraint_policy': policy} if policy else {})}
    async def run():
        paper = paper_engine(cloud)
        plan = await inputs(cloud, paper, base)
        cloud.clock[0] = f.at(4, 10)
        research = engine(cloud)
        bundle = await research.register('shared-reference-policy-v1', base, configs())
        job = await research.enqueue(bundle['bundle_id'], f.RACE, plan['asof_at'])
        assert (await research.tick())['status'] == 'COMPLETE'
        output = (await research.result(job['job_id'], cloud.clock[0]))['result']['output']
        decisions = (await paper.decide(plan['id']))['decisions']
        for decision in decisions:
            choice = output['candidates']['native_' + decision['model']]
            if policy == 'allow_inconsistent_shadow':
                assert choice['stake_yen'] == decision['stake_yen']
                assert [t['selection'] for t in choice['tickets']] == ([decision['selection']] if decision['selection'] else [])
                assert output['research_assumptions'] == ['INCONSISTENT_REFERENCES_SOFT_CALIBRATION']
                assert decision['research_assumptions'].count(output['research_assumptions'][0]) == 1
            else:
                assert choice['reason'] == decision['reason'] == 'REFERENCE_INCONSISTENT'
                assert choice['stake_yen'] == decision['stake_yen'] == 0 and not choice['tickets']
        assert (await paper.decide(plan['id']))['decisions'] == decisions
        assert (await research.result(job['job_id'], cloud.clock[0]))['result']['output'] == output
    asyncio.run(run())


def test_shared_91_choices_immutable_bundle_job_and_publication(cloud, config):
    async def run():
        paper = paper_engine(cloud)
        plan = await inputs(cloud, paper, config)
        cloud.clock[0] = f.at(4, 10)
        h = engine(cloud)
        bundle = await h.register('synthetic-v1', config, configs())
        assert await h.register('synthetic-v1', config, configs()) == bundle
        with pytest.raises(ValueError, match='BUNDLE_VERSION_CONFLICT'):
            await h.register('synthetic-v1', {**config, 'lambda': 0.02}, configs())
        job = await h.enqueue(bundle['bundle_id'], f.RACE, plan['asof_at'])
        cutoff = cloud.clock[0]
        cloud.clock[0] = f.at(5)
        completed = await h.tick()
        assert completed['status'] == 'COMPLETE' and completed['attempts'] == 1
        saved = await h.result(job['job_id'], f.at(5))
        data = saved['result']
        assert len(data['output']['candidates']) == 91
        assert not data['paper_eligible'] and not data['timing_qualified']
        assert data['settlement_status'] == 'NOT_EVALUATED'
        snapshot = await h.read_body(data['input_hash'])
        assert all(x['available_at'] <= job['asof_at'] for x in snapshot['view']['markets'].values())
        assert snapshot['view']['markets']['win']['content']['source_updated_at'] is None
        past = await h.result(job['job_id'], cutoff)
        assert past['result'] is None and 'result_hash' not in past['job']
        assert (await h.tick())['status'] == 'IDLE'
        assert (await h.enqueue(bundle['bundle_id'], f.RACE, plan['asof_at'])) == completed
        assert await paper.decision(plan['id']) == {'plan_id': plan['id'], 'decisions': None, 'details_hash': None, 'decision_at': None}
        # Later final-only content cannot change a completed job or past input.
        cloud.clock[0] = f.at(8)
        await h.odds.normalize(cloud.seed(55, 7, f.archive(distorted=False), 'FINAL_ONLY'))
        assert (await h.result(job['job_id'], f.at(8)))['result'] == data
        assert (await h.jobs(bundle['bundle_id'])) == [completed]
    asyncio.run(run())


def test_missing_old_input_excludes_all_choices_despite_later_original(cloud, config):
    async def run():
        h = engine(cloud)
        bundle = await h.register('missing-v1', config, configs())
        job = await h.enqueue(bundle['bundle_id'], f.RACE, f.at(0))
        await h.odds.normalize(cloud.seed(9, 2))
        done = await h.tick(lambda *_: pytest.fail('missing data cannot be analyzed'))
        assert done['status'] == 'COMPLETE'
        output = (await h.result(job['job_id'], f.at(5)))['result']['output']
        assert len(output['candidates']) == 91
        assert all(c['status'] == 'INPUT_EXCLUDED' and not c['tickets'] for c in output['candidates'].values())
        assert cloud.db.conn.execute('SELECT COUNT(*) FROM cloud_paper_plans').fetchone()[0] == 0
    asyncio.run(run())


def test_expired_claim_is_bounded_and_foreign_engine_never_runs(cloud, config):
    async def run():
        h = engine(cloud)
        b = await h.register('leases-v1', config, configs())
        job = await h.enqueue(b['bundle_id'], f.RACE, f.at(0))
        cloud.db.conn.execute("UPDATE cloud_research_jobs SET status='RUNNING',owner='lost',lease_until=?,attempts=2 WHERE job_id=?",
                              (stamp(f.at(1)), job['job_id']))
        cloud.db.conn.commit()
        assert (await h.tick())['status'] == 'IDLE'
        saved = await h.result(job['job_id'], f.at(5))
        assert saved['job']['status'] == 'FAILED' and saved['result'] is None
        other = await h.enqueue(b['bundle_id'], f.RACE, f.at(2))
        cloud.db.conn.execute("UPDATE cloud_research_jobs SET status='RUNNING',owner='old-engine',lease_until=?,attempts=1 WHERE job_id=?",
                              (stamp(f.at(1)), other['job_id']))
        cloud.db.conn.commit()
        h.engine_id = 'b' * 64
        assert (await h.tick())['status'] == 'ENGINE_UNAVAILABLE'
        assert (await h.result(other['job_id'], f.at(5)))['result'] is None
        assert cloud.db.conn.execute('SELECT SUM(attempts) FROM cloud_research_jobs').fetchone()[0] == 3
        assert (await h.tick())['status'] == 'IDLE'
    asyncio.run(run())


def test_failed_output_does_not_publish_or_stop_collection(cloud, config):
    async def run():
        h = engine(cloud)
        b = await h.register('failure-v1', config, configs())
        job = await h.enqueue(b['bundle_id'], f.RACE, f.at(0))
        cloud.db.fail = "UPDATE cloud_research_jobs SET status='COMPLETE'"
        done = await h.tick()
        assert done['status'] == 'FAILED' and done['result_hash'] is None
        assert (await h.result(job['job_id'], f.at(5)))['result'] is None
        assert cloud.db.conn.execute('SELECT blocked FROM source_control').fetchone()[0] == 0
        assert cloud.db.conn.execute('SELECT COUNT(*) FROM raw_observations').fetchone()[0] == 0
    asyncio.run(run())


def test_trend_keeps_earlier_full_observation_and_rejects_zero():
    from hr_platform.cloud_research import trend_options
    from hr_platform.common import canonical
    old = {'observation_id': 'before', 'received_at': f.at(0), 'content': {'state': {'status': 'UNKNOWN'},
           'quotes': {'1-2': {'odds': 12, 'display_status': 'FIXED'}}}}
    new = {**deepcopy(old), 'observation_id': 'now', 'received_at': f.at(2)}
    saved = {'view': {'markets': {'quinella': new}}, 'previous': {'reason': None, 'markets': {'quinella': old}}}
    before = identity(saved)
    assert trend_options(saved, {'target': 'quinella'}) == {'previous_quotes': {'1-2': 12}, 'elapsed_seconds': 120}
    assert identity(saved) == before
    old['content']['quotes']['1-2']['odds'] = 0
    assert trend_options(saved, {'target': 'quinella'})['previous_quotes'] is None
    assert canonical(saved)


def test_separate_observation_does_not_join_the_latest_snapshot(cloud, config):
    async def run():
        p = paper_engine(cloud)
        plan = await inputs(cloud, p, config)
        cloud.clock[0] = f.at(4, 10)
        h = engine(cloud)
        b = await h.register('observation-boundary-v1', config, configs())
        bundle = await h.read_body(b['body_hash'])
        job = await h.enqueue(b['bundle_id'], f.RACE, plan['asof_at'])
        original = h.odds.asof
        async def fragmented(race, markets, at, max_age):
            view = await original(race, markets, at, max_age)
            if 'win' in markets:
                view['markets']['quinella']['observation_id'] = 'separate-old-observation'
                view['markets']['quinella']['received_at'] = f.at(1, 59)
            return view
        h.odds.asof = fragmented
        saved = await h.input(job, bundle)
        assert 'quinella' in saved['observed_view']['markets']
        assert 'quinella' not in saved['view']['markets']
        assert len({r['observation_id'] for r in saved['view']['markets'].values()}) == 1
    asyncio.run(run())


def test_prepared_engine_changes_with_dependencies_and_keeps_private_defaults(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    import prepare_python_worker
    import prepare_backtest_worker
    monkeypatch.setattr(prepare_python_worker, 'private_root', lambda p: Path(p))
    one = prepare_backtest_worker.prepare_backtest(tmp_path / 'first')
    original = prepare_backtest_worker.prepare
    def changed_dependencies(*args, **kwargs):
        root = original(*args, **kwargs)
        lock = root / 'pylock.toml'
        lock.write_text(lock.read_text().replace('2.4.6', '2.4.7'))
        return root
    monkeypatch.setattr(prepare_backtest_worker, 'prepare', changed_dependencies)
    two = prepare_backtest_worker.prepare_backtest(tmp_path / 'second')
    first, second = (json.loads((p / 'engine-manifest.json').read_bytes()) for p in (one, two))
    assert first['basis']['sources'] == second['basis']['sources']
    assert first['basis']['dependencies'] != second['basis']['dependencies']
    assert first['engine_id'] != second['engine_id']
    cfg = json.loads((two / 'wrangler.jsonc').read_bytes())
    assert cfg['name'] == 'hr-platform-dev-backtest' and not cfg['workers_dev'] and not cfg['preview_urls']
    assert cfg['triggers']['crons'] == [] and cfg['vars']['RESEARCH_ENABLED'] == 'false'
    assert 'services' not in cfg and 'durable_objects' not in cfg
