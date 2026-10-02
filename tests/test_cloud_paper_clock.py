"""Exercise private timer RPCs against the shared synthetic Paper store."""
import asyncio
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from hr_platform import fixtures as f
from hr_platform.common import instant, stamp
from test_cloud_paper import cloud as cloud_fixture, engine, inputs

cloud = cloud_fixture


class Storage:
    def __init__(self):
        self.at = None

    async def getAlarm(self):
        return self.at

    async def setAlarm(self, at):
        self.at = at

    async def deleteAlarm(self):
        self.at = None


@pytest.fixture
def entry(monkeypatch):
    class Base:
        def __init__(self, ctx, env):
            self.ctx, self.env = ctx, env
    monkeypatch.setitem(sys.modules, 'workers', SimpleNamespace(
        DurableObject=Base, WorkerEntrypoint=Base, Response=None))
    spec = importlib.util.spec_from_file_location('research_entry', 'workers/research/entry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('outcome', ['on_time', 'late', 'postponed'])
def test_alarm_waits_rechecks_and_preserves_fixed_decision(cloud, config, monkeypatch, entry, outcome):
    async def run():
        config['max_decision_delay_seconds'] = 30
        paper = engine(cloud)
        plan = await inputs(cloud, paper, config)
        monkeypatch.setattr(entry, 'paper_engine', lambda _: paper)
        worker = entry.Default(None, SimpleNamespace(PAPER_ENABLED='true'))
        assert await worker.paper_next_alarm() == round(instant(f.at(3, 55)).timestamp() * 1000)
        # A saved postponement/advance updates the one alarm, not a second plan.
        metadata = paper.metadata
        calls = []
        async def wait(delay):
            calls.append(delay)
            assert (await paper.decision(plan['id']))['decisions'] is None
            cloud.clock[0] = f.at(4, 31) if outcome == 'late' else f.at(4)
            if outcome == 'postponed':
                async def changed(race, at):
                    result = await metadata(race, at)
                    result['evidence']['metadata']['scheduled_start_at'] = f.at(18)
                    result['evidence']['available_at'] = stamp(f.at(3, 59))
                    return result
                paper.metadata = changed
        monkeypatch.setattr('hr_platform.cloud_paper.asyncio.sleep', wait)
        cloud.clock[0] = f.at(3, 55)
        await worker.paper_tick()
        assert calls == [5]
        result = await paper.decision(plan['id'])
        if outcome == 'postponed':
            assert result['decisions'] is None
            assert await worker.paper_next_alarm() == round(instant(f.at(7, 55)).timestamp() * 1000)
            assert (await paper.plan(plan['id'], f.at(0))) == plan
        else:
            assert await worker.paper_next_alarm() is None
            assert all(d['status'] == ('PAPER_BET' if outcome == 'on_time' else 'NO_BET')
                       for d in result['decisions'])
            await worker.paper_tick()  # At-least-once delivery cannot purchase again.
            assert await paper.decision(plan['id']) == result
            assert calls == [5]
    asyncio.run(run())


def test_timer_uses_revision_and_lease_and_can_be_disabled(cloud, config, monkeypatch, entry):
    async def run():
        cloud.clock[0] = f.at(0)
        paper = engine(cloud)
        assert await paper.next_alarm() is None
        await inputs(cloud, paper, config)
        monkeypatch.setattr(entry, 'paper_engine', lambda _: paper)
        env = SimpleNamespace(PAPER_ENABLED='true')
        worker = entry.Default(None, env)
        old = await worker.paper_next_alarm()
        cloud.db.conn.execute('UPDATE cloud_paper_plans SET asof_at=?', (stamp(f.at(5)),))
        cloud.db.conn.commit()
        assert await worker.paper_next_alarm() == old + 60000
        cloud.db.conn.execute('UPDATE cloud_paper_plans SET owner=?,lease_until=?', ('active', stamp(f.at(8))))
        cloud.db.conn.commit()
        assert await worker.paper_next_alarm() == round(instant(f.at(8)).timestamp() * 1000)
        env.PAPER_ENABLED = 'false'
        await worker.paper_tick()
        assert await worker.paper_next_alarm() is None
    asyncio.run(run())


def test_tick_error_propagates_for_retry_and_retired_clock_never_loads_engine(monkeypatch, entry):
    async def run():
        async def failure(**_):
            raise RuntimeError('synthetic storage failure')
        monkeypatch.setattr(entry, 'paper_engine', lambda _: SimpleNamespace(tick=failure))
        worker = entry.Default(None, SimpleNamespace(PAPER_ENABLED='true'))
        with pytest.raises(RuntimeError, match='synthetic'):
            await worker.paper_tick()
        monkeypatch.setattr(entry, 'paper_engine', lambda _: pytest.fail('retired timer loaded engine'))
        storage = Storage()
        storage.at = 1234
        timer = entry.PaperClock(SimpleNamespace(storage=storage), worker.env)
        await timer.alarm()
        assert storage.at is None
    asyncio.run(run())
