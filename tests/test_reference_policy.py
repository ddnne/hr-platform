"""Research policy tests use inconsistent synthetic prices and real numerical solvers."""
from pathlib import Path
import pytest
import yaml
from hr_platform import fixtures as f
from hr_platform.common import canonical
from hr_platform.evaluation import compare
from hr_platform.paper import decide, settle
from hr_platform.research import research_asof


@pytest.fixture
def inconsistent(store, monkeypatch):
    original = f.markets
    def changed(*args, **kwargs):
        markets = original(*args, **kwargs)
        markets['win']['quotes']['1']['odds'] *= 0.5
        return markets
    monkeypatch.setattr(f, 'markets', changed)
    store.ingest(f.event('inconsistent', 2), f.archive())
    store.clock = lambda: f.at(4, 20)
    return store


def shadow(config):
    return {**config, 'version': config['version'] + '-shadow',
            'reference_constraint_policy': 'allow_inconsistent_shadow'}


def test_shadow_keeps_inconsistency_and_strict_history(inconsistent, config):
    old = decide(inconsistent, f.RACE, f.schedule(), config)
    assert all(d['reason'] == 'REFERENCE_INCONSISTENT' and d['stake_yen'] == 0 for d in old)
    policy = shadow(config)
    new = decide(inconsistent, f.RACE, f.schedule(), policy)
    for decision in new:
        diagnostic = decision['diagnostics']
        assert diagnostic['reference_diagnostics']['status'] == 'optimal'
        assert diagnostic['identification'] == {
            'status': 'INCONSISTENT', 'lower': None, 'upper': None,
            'epsilon': config['identification_epsilon']}
        assert diagnostic['reference_consistency']['minimum_uniform_absolute_slack'] > config['identification_epsilon']
        assert decision['research_assumptions'] == ['INCONSISTENT_REFERENCES_SOFT_CALIBRATION']
        assert decision['reference_constraint_policy'] == 'allow_inconsistent_shadow'
        assert decision['reason'] in {None, 'NO_EDGE'}
        assert decision['real_stake_yen'] == 0
    assert any(d['status'] == 'PAPER_BET' for d in new)
    assert all(d['input_view'] == old[0]['input_view'] for d in new)
    assert canonical(decide(inconsistent, f.RACE, f.schedule(), policy)) == canonical(new)
    assert canonical(decide(inconsistent, f.RACE, f.schedule(), config)) == canonical(old)
    assert inconsistent.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 6
    before = compare(inconsistent, policy, f.at(5))
    for summary in before['models'].values():
        assert summary['reference_constraint_status_counts'] == {'INCONSISTENT': 1}
        assert summary['research_assumption_counts'] == {'INCONSISTENT_REFERENCES_SOFT_CALIBRATION': 1}
        assert summary['entries'][0]['reference_constraint_status'] == 'INCONSISTENT'
        if summary['bet_count']:
            assert summary['profit_yen'] is None
    inconsistent.clock = lambda: f.at(21)
    for decision in new:
        settle(inconsistent, decision['id'], f.payout())
    after = compare(inconsistent, policy, f.at(22))
    assert all(s['pending_count'] == 0 for s in after['models'].values())
    assert all(s['reference_constraint_status_counts'] == {'INCONSISTENT': 1} for s in after['models'].values())
    # Later synthetic settlement never changes the earlier decision or payout view.
    assert compare(inconsistent, policy, f.at(5))['models'] == before['models']
    assert canonical(decide(inconsistent, f.RACE, f.schedule(), config)) == canonical(old)


def test_policy_change_requires_new_experiment(inconsistent, config):
    decide(inconsistent, f.RACE, f.schedule(), config)
    with pytest.raises(ValueError, match='EXPERIMENT_CONFIG_CHANGED'):
        decide(inconsistent, f.RACE, f.schedule(), {**shadow(config), 'version': config['version']})


@pytest.mark.parametrize('change', [
    {'reference_constraint_policy': 'typo'},
    {'reference_constraint_policy': 'allow_inconsistent_shadow', 'mode': 'FROZEN_PAPER'},
])
def test_unknown_or_frozen_shadow_policy_is_rejected(collected, config, change):
    with pytest.raises(ValueError, match='REFERENCE_CONSTRAINT_POLICY'):
        decide(collected, f.RACE, f.schedule(), {**config, **change})
    assert collected.db.execute('SELECT count(*) FROM experiments').fetchone()[0] == 0


def test_shadow_still_stops_actual_solver_failure(inconsistent, config):
    policy = {**shadow(config), 'solver_max_iter': 1}
    records = decide(inconsistent, f.RACE, f.schedule(), policy)
    assert all(d['reason'] == 'MODEL_ERROR' and d['stake_yen'] == 0 for d in records)
    inconsistent.clock = lambda: f.at(6, 2)
    inconsistent.ingest(f.event('next', 6), f.archive())
    assert inconsistent.metrics()['observations'] == 2


def test_shadow_does_not_bypass_freshness_or_deadline(inconsistent, config):
    policy = {**shadow(config), 'max_age_seconds': 1}
    stale = decide(inconsistent, f.RACE, f.schedule(), policy)
    assert all(d['reason'] == 'STALE' and d['diagnostics'] is None for d in stale)
    moments = iter([f.at(4, 10), f.at(7), f.at(7)])
    late = decide(inconsistent, f.RACE, f.schedule(),
                  {**shadow(config), 'version': 'late-shadow'}, clock=lambda: next(moments))
    assert all(d['reason'] == 'DECISION_TOO_LATE' and d['stake_yen'] == 0 for d in late)


def test_retrospective_inconsistency_remains_a_diagnostic(inconsistent, config):
    result = research_asof(inconsistent, f.RACE, f.schedule(), shadow(config))
    assert result['status'] == 'REFERENCE_INCONSISTENT'
    assert result['analysis']['reference_diagnostics']['status'] == 'optimal'
    assert result['paper_decision_created'] is False
    assert inconsistent.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 0


def test_published_shadow_changes_only_version_and_reference_policy(config):
    new = yaml.safe_load(Path('configs/research-shadow.yaml').read_text())
    assert new.pop('reference_constraint_policy') == 'allow_inconsistent_shadow'
    assert new.pop('version') != config['version']
    assert new == {k:v for k,v in config.items() if k != 'version'}
