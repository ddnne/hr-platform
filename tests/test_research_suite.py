import copy
import json
from pathlib import Path

import numpy as np
import pytest

from hr_platform import fixtures
from hr_platform.model import ModelError, analyze, matrix, reference, states
from hr_platform.research_alternatives import estimate
from hr_platform.research_suite import candidate_suite


def configs():
    return {n: json.loads(Path('configs/research-' + n + '.json').read_text()) for n in
            ('successors', 'portfolio', 'portfolio-scenarios', 'expansions', 'alternatives', 'kelly',
             'joint-kelly', 'all-markets-kelly')}


def test_pair_marginal_pl_reuses_observed_probabilities_without_fabricating_win_market(config):
    markets = fixtures.markets('nonuniform')
    del markets['win']
    base = {**config, 'references': ['exacta', 'trifecta']}
    saved = analyze([1, 2, 3, 4], markets, base)
    before = copy.deepcopy(markets)
    result = estimate([1, 2, 3, 4], markets, saved, base, configs()['alternatives'],
                      win_probability_source='exacta_first_marginal')
    omega = states([1, 2, 3, 4])
    _, pair_event, neutral_pair, _, _ = reference(omega, 'exacta', markets['exacta']['quotes'])
    pair_first = np.zeros(len(omega))
    for i, (a, _, _) in enumerate(omega):
        pair_first[i] = sum(p for (x, _), p in zip(matrix(omega, 'exacta')[0], neutral_pair) if x == a)
    _, win_event = matrix(omega, 'win')
    expected = np.array([pair_first[omega.index((h, *[x for x in range(1, 5) if x != h][:2]))] for h in range(1, 5)])
    np.testing.assert_allclose(win_event @ result['estimators']['win_pl']['q'], expected)
    assert len(result['estimators']) == 7 and markets == before and 'win' not in markets
    for model in result['estimators'].values():
        assert sum(model['q']) == pytest.approx(1)
    changed = copy.deepcopy(markets)
    changed['quinella']['quotes'].clear()
    assert estimate([1, 2, 3, 4], changed, saved, base, configs()['alternatives'],
                    win_probability_source='exacta_first_marginal') == result
    changed['exacta']['quotes'].pop('1-2')
    with pytest.raises(ModelError, match='INCOMPLETE_MARKET'):
        estimate([1, 2, 3, 4], changed, saved, base, configs()['alternatives'],
                 win_probability_source='exacta_first_marginal')


def test_native_offered_markets_share_all_91_choices_and_leave_missing_trend_excluded(config):
    markets = fixtures.markets('uniform')
    del markets['win']
    markets['wide'] = {'quotes': {s: {'odds': 1.05, 'odds_max': 1.1, 'display_status': 'RANGE'}
                                  for s in markets['quinella']['quotes']}}
    base = {**config, 'references': ['exacta', 'trifecta']}
    saved = analyze([1, 2, 3, 4], markets, base)
    original = copy.deepcopy((saved, markets))
    result = candidate_suite([1, 2, 3, 4], markets, saved, base, configs(), actual_offered=list(markets),
                             place_places=3, win_probability_source='exacta_first_marginal', baseline_regularization=1e-8)
    assert len(result['candidates']) == 91 and result['family_exclusions'] == {}
    assert result['finite_baseline']['basis'] == 'FINITE_POSITIVE_KL_PROXY_NOT_ANALYTIC_ZERO_LIMIT'
    assert result['candidates']['consensus_log_growth_trend']['status'] == 'INPUT_EXCLUDED'
    assert result['candidates']['trend_damped_joint']['status'] == 'INPUT_EXCLUDED'
    for choice in result['all_market_diagnostics']['candidates'].values():
        assert set(choice['offered_markets']) == set(markets)
        assert not set(choice['calibration_markets']) & {t['market'] for t in choice['tickets']}
    assert (saved, markets) == original
    with pytest.raises(ModelError, match='FINITE_BASELINE_CONFIG_REQUIRED'):
        candidate_suite([1, 2, 3, 4], markets, saved, base, configs(), actual_offered=list(markets))


def test_unusable_quinella_does_not_drop_valid_trio_family(config):
    from hr_platform.research_suite import analyze_suite
    markets = fixtures.markets('uniform')
    markets['quinella']['quotes']['1-2'] = {'odds': 0, 'odds_max': None, 'display_status': 'UNKNOWN'}
    original = copy.deepcopy(markets)
    result = analyze_suite([1, 2, 3, 4], markets, config, configs(), actual_offered=list(markets), place_places=2)
    assert result['primary_error'] == 'UNUSABLE_ODDS'
    assert len(result['candidates']) == 91
    assert sum(c.get('status') != 'INPUT_EXCLUDED' for c in result['candidates'].values()) == 3
    assert all(result['candidates']['trio_' + m].get('status') != 'INPUT_EXCLUDED'
               for m in ('direct', 'marginal', 'reference'))
    assert markets == original


def test_failed_finite_baseline_keeps_primary_and_independent_candidates(config, monkeypatch):
    from hr_platform import research_suite
    markets = fixtures.markets('uniform')
    del markets['win']
    base = {**config, 'references': ['exacta', 'trifecta']}
    primary = analyze([1, 2, 3, 4], markets, base)

    def fail_baseline(*args, **kwargs):
        raise ModelError('FINITE_BASELINE_SOLVER_FAILED')

    monkeypatch.setattr(research_suite, 'fit', fail_baseline)
    result = research_suite.analyze_suite([1, 2, 3, 4], markets, base, configs(),
                                          win_probability_source='exacta_first_marginal',
                                          actual_offered=list(markets), baseline_regularization=1e-8)
    assert result['primary_error'] is None
    np.testing.assert_allclose(result['primary_analysis']['q_ref'], primary['q_ref'])
    assert result['primary_analysis']['reference_diagnostics']['references'] == base['references']
    assert result['family_exclusions']['finite_baseline'] == 'FINITE_BASELINE_SOLVER_FAILED'
    assert len(result['candidates']) == 91
    for name in ('reference_sensitivity_floor', 'consensus_log_growth', 'single_300'):
        assert result['candidates'][name]['status'] == 'INPUT_EXCLUDED'
    for name in ('reference_baseline', 'native_reference', 'reference:max_ev_single',
                 'kelly_reference_full', 'joint_kelly_reference_half', 'trio_reference'):
        assert result['candidates'][name].get('status') != 'INPUT_EXCLUDED'


@pytest.mark.parametrize('n,places,no_win', [(6, 2, False), (8, 3, False), (9, 3, True)])
def test_sport_place_rules_and_frame_support_use_same_joint_payoffs(config, n, places, no_win):
    from hr_platform.model import key
    from hr_platform.research_ticket_events import ticket_catalog, winning_selections
    runners = list(range(1, n + 1))
    omega = states(runners)
    frames = {h: min(h, 8) for h in runners}
    offered = (['exacta', 'quinella', 'trifecta', 'trio', 'wide', 'bracket_exacta', 'bracket_quinella']
               if no_win else ['win', 'place', 'exacta', 'quinella', 'trifecta', 'trio', 'wide'])
    markets = {}
    for market in offered:
        outcomes = [winning_selections(s, market, frames=frames, place_places=places) for s in omega]
        selections = sorted({s for row in outcomes for s in row})
        quotes = {}
        for selection in selections:
            price = 0.8 / (sum(selection in row for row in outcomes) / len(omega))
            ranged = market in {'place', 'wide'}
            quotes[key(selection)] = {'odds': price, 'odds_max': price * 1.1 if ranged else None,
                                       'display_status': 'RANGE' if ranged else 'FIXED'}
        markets[market] = {'quotes': quotes}
    base = {**config, 'references': ['exacta', 'trifecta'] if no_win else ['exacta']}
    saved = analyze(runners, markets, base)
    options = configs()
    options['all-markets-kelly']['candidates_per_market'] = 1
    options['joint-kelly']['candidates_per_market'] = 1
    options['portfolio-scenarios']['race_budget_yen'] = 100
    result = candidate_suite(runners, markets, saved, base, options, actual_offered=offered, frames=frames,
                             place_places=places, win_probability_source='exacta_first_marginal' if no_win else 'market',
                             baseline_regularization=1e-8)
    assert len(result['candidates']) == 91
    expected_refs = set(base['references']) | {'exacta'} | (set() if no_win else {'win'})
    assert set(result['used_reference_markets']) == expected_refs
    for choice in result['all_market_diagnostics']['candidates'].values():
        assert choice['place_paid_positions'] == places
        assert set(choice['offered_markets']) == set(offered)
        if choice['reference_family'] == 'primary':
            assert set(choice['calibration_markets']) == expected_refs
        assert not set(choice['calibration_markets']) & {t['market'] for t in choice['tickets']}
    for market in offered:
        _, event, odds, _ = ticket_catalog(omega, market, markets[market]['quotes'], frames=frames, place_places=places)
        np.testing.assert_allclose((event @ np.full(len(omega), 1 / len(omega))) * odds, 0.8)
