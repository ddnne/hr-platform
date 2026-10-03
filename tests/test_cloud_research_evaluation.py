"""Saved synthetic bindings only; no provider or actual Cloudflare requests."""
import asyncio
from copy import deepcopy

import pytest
from hr_platform import fixtures as f
from hr_platform.common import canonical, stamp
from hr_platform.research_suite import strategy_names
from test_cloud_history import cloud as cloud_fixture
from test_cloud_pages import seed as seed_page
from test_cloud_paper import engine as paper_engine, inputs
from test_cloud_research import engine
from test_research_evaluation import ordinary_page
from test_research_suite import configs
from test_realdata import race_archive

cloud = cloud_fixture


async def completed(c, config):
    p = paper_engine(c)
    plan = await inputs(c, p, config)
    c.clock[0] = f.at(5)
    h = engine(c)
    b = await h.register('SYNTHETIC-EVALUATION', config, configs())
    j = await h.enqueue(b['bundle_id'], f.RACE, plan['asof_at'])
    def fixed(*args, **kwargs):
        candidates = {n: {'status': 'INPUT_EXCLUDED', 'reason': 'SYNTHETIC_EXCLUDED',
                         'tickets': [], 'stake_yen': 0} for n in strategy_names(configs())}
        candidates['native_direct'] = {'tickets': [{'selection': s, 'stake_yen': stake}
            for s, stake in [('1-3', 200), ('1-2', 100)]], 'stake_yen': 300}
        return {'candidates': candidates}
    assert (await h.tick(fixed))['status'] == 'COMPLETE'
    return h, j, await h.result(j['job_id'], c.clock[0])


def test_cash_final_price_revisions_replay_without_refit_or_paper_writes(cloud, config):
    async def run():
        h, job, original = await completed(cloud, config)
        before_input = await h.read_body(original['job']['input_hash'])
        pending = await h.evaluate(job['job_id'], f.at(5))
        s = pending['evaluation']['strategies']['native_direct']['summary']
        assert s['profit_yen'] is None and s['average_final_odds'] is None and s['pending_ticket_count'] == 2
        assert len(pending['evaluation']['strategies']) == 91
        old = deepcopy(pending)
        writes = cloud.bucket.writes
        cloud.clock[0] = f.at(6)
        assert await h.evaluate(job['job_id'], f.at(6)) == pending
        assert cloud.bucket.writes == writes
        assert (await h.evaluation(pending['record']['evaluation_id'], f.at(4)))['evaluation'] is None
        cloud.clock[0] = f.at(21)
        await h.pages.normalize(seed_page(cloud, 20, 20, 'payout', ordinary_page()))
        cash = await h.evaluate(job['job_id'], f.at(21))
        assert cash['record']['evaluation_id'] != pending['record']['evaluation_id']
        s = cash['evaluation']['strategies']['native_direct']['summary']
        assert (s['stake_yen'], s['payout_yen'], s['profit_yen']) == (300, 1300, 1000)
        assert s['average_final_odds'] is None
        cloud.clock[0] = f.at(22)
        await h.odds.normalize(cloud.seed(21, 21, f.archive(distorted=False), 'FINAL_ONLY'))
        priced = await h.evaluate(job['job_id'], f.at(22))
        s = priced['evaluation']['strategies']['native_direct']['summary']
        assert s['final_odds_known_ticket_count'] == 2 and s['average_final_odds'] > 1
        assert s['profit_yen'] == 1000
        assert not priced['evaluation']['paper_eligible'] and not priced['evaluation']['daily_budget_applied']
        assert (await h.evaluation(priced['record']['evaluation_id'], f.at(21)))['evaluation'] is None
        assert await h.evaluate(job['job_id'], f.at(21)) == cash
        assert await h.evaluation(pending['record']['evaluation_id'], f.at(5)) == {
            **old, 'asof_at': stamp(f.at(5)), 'paper_eligible': False}
        assert await h.result(job['job_id'], f.at(22)) == {**original, 'asof_at': stamp(f.at(22))}
        assert await h.read_body(original['job']['input_hash']) == before_input
        assert await h.input(original['job'], await h.read_body(original['job']['bundle_id'])) == before_input
        assert cloud.db.conn.execute('SELECT count(*) FROM cloud_research_evaluations').fetchone()[0] == 3
        assert cloud.db.conn.execute('SELECT count(*) FROM cloud_paper_settlements').fetchone()[0] == 0
        assert cloud.db.conn.execute('SELECT decisions FROM cloud_paper_plans').fetchone()[0] is None
    asyncio.run(run())


def test_delayed_evaluation_publication_and_read_from_new_engine_keep_old_result(cloud, config):
    async def run():
        h, job, original = await completed(cloud, config)
        save = h.save_body
        async def delayed(data):
            digest = await save(data)
            cloud.clock[0] = f.at(7)
            return digest
        h.save_body = delayed
        h.engine_id = 'b' * 64
        evaluation = await h.evaluate(job['job_id'], f.at(5))
        assert evaluation['record']['available_at'] == stamp(f.at(7))
        assert evaluation['evaluation']['evidence_asof_at'] == stamp(f.at(5))
        assert evaluation['evaluation']['evaluator_engine_id'] == 'b' * 64
        assert (await h.evaluation(evaluation['record']['evaluation_id'], f.at(6)))['evaluation'] is None
        assert canonical((await h.result(job['job_id'], f.at(7)))['result']) == canonical(original['result'])
        with pytest.raises(ValueError, match='RESEARCH_RESULT_UNAVAILABLE'):
            await h.evaluate(job['job_id'], f.at(4))
    asyncio.run(run())


def test_failed_index_publication_retries_without_exposing_or_overwriting_result(cloud, config):
    async def run():
        h, job, original = await completed(cloud, config)
        cloud.db.fail = 'INSERT OR IGNORE INTO cloud_research_evaluations'
        with pytest.raises(RuntimeError, match='injected write failure'):
            await h.evaluate(job['job_id'], f.at(5))
        assert cloud.db.conn.execute('SELECT count(*) FROM cloud_research_evaluations').fetchone()[0] == 0
        cloud.db.fail = None
        cloud.clock[0] = f.at(6)
        repaired = await h.evaluate(job['job_id'], f.at(6))
        assert repaired['record']['available_at'] == stamp(f.at(6))
        assert (await h.evaluation(repaired['record']['evaluation_id'], f.at(5)))['evaluation'] is None
        assert (await h.result(job['job_id'], f.at(6)))['result'] == original['result']
    asyncio.run(run())


def test_missing_race_in_existing_metadata_archive_keeps_all_exclusion_reasons(cloud, config):
    async def run():
        h = engine(cloud)
        cloud.clock[0] = f.at(5)
        raw = race_archive()
        # Rewrite all CSV members, retaining a valid archive for another venue.
        import io
        import zipfile
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, 'w') as target:
            for member in source.namelist():
                target.writestr(member, source.read(member).replace(b'SYNTHETIC', b'SYNTHETIC_OTHER'))
        await h.races.normalize(cloud.seed(40, 0, output.getvalue(), 'NAR_RACE_BUNDLE', 'nar-daily-race'))
        b = await h.register('SYNTHETIC-MISSING-METADATA', config, configs())
        job = await h.enqueue(b['bundle_id'], f.RACE, f.at(5))
        assert (await h.tick(lambda *_: pytest.fail('must retain missing input')))['status'] == 'COMPLETE'
        saved = await h.read_body((await h.result(job['job_id'], f.at(5)))['job']['input_hash'])
        assert saved['metadata']['evidence'] and saved['metadata']['evidence']['metadata'] is None
        cloud.clock[0] = f.at(21)
        await h.pages.normalize(seed_page(cloud, 41, 20, 'payout', ordinary_page()))
        result = await h.evaluate(job['job_id'], f.at(21))
        assert len(result['evaluation']['strategies']) == 91
        assert all(s['status'] == 'INPUT_EXCLUDED' and s['reason'] == 'METADATA_MISSING'
                   and s['summary']['eligible_race_count'] == 0 for s in result['evaluation']['strategies'].values())
    asyncio.run(run())
