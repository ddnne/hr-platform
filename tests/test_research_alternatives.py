import copy
from itertools import combinations
import json
from pathlib import Path

import numpy as np
import pytest

from hr_platform import fixtures
from hr_platform.model import analyze, key, matrix, reference, states, ModelError
from hr_platform.research_alternatives import estimate, purchases


def settings(config):
    return (config, json.loads(Path('configs/research-successors.json').read_text()),
            json.loads(Path('configs/research-portfolio-scenarios.json').read_text()),
            json.loads(Path('configs/research-alternatives.json').read_text()))


def test_new_distributions_have_analytic_marginals_and_exclude_target(config):
    base, _, _, options = settings(config)
    markets = fixtures.markets('nonuniform')
    saved = analyze([1, 2, 3, 4], markets, base)
    result = estimate([1, 2, 3, 4], markets, saved, base, options)
    omega = states([1, 2, 3, 4])
    _, aw, win, _, _ = reference(omega, 'win', markets['win']['quotes'])
    _, ap, pair, _, _ = reference(omega, 'exacta', markets['exacta']['quotes'])
    assert len(result['estimators']) == 7
    for row in result['estimators'].values():
        assert len(row['q']) == 24 and min(row['q']) >= 0
        assert sum(row['q']) == pytest.approx(1)
        assert sum(row['probabilities']) == pytest.approx(1)
    for name in ('win_pl', 'win_exacta_chain'):
        np.testing.assert_allclose(aw @ result['estimators'][name]['q'], win)
    tempered = pair ** options['exacta_power']
    np.testing.assert_allclose(ap @ result['estimators']['exacta_power']['q'], tempered / tempered.sum())
    first = omega.index((1, 2, 3))
    assert result['estimators']['win_pl']['q'][first] == pytest.approx(
        win[0] * win[1] / sum(win[1:]) * win[2] / sum(win[2:]))
    changed = copy.deepcopy(markets)
    changed['quinella']['quotes'].clear()
    assert estimate([1, 2, 3, 4], changed, saved, base, options) == result
    before = copy.deepcopy(result)
    with pytest.raises(ModelError, match='INCOMPLETE_MARKET'):
        changed['exacta']['quotes'].pop('1-2')
        estimate([1, 2, 3, 4], changed, saved, base, options)
    assert result == before


def test_every_method_is_uniform_on_uniform_markets_and_control_prices_match(config):
    base, successors, portfolio, options = settings(config)
    markets = fixtures.markets('uniform')
    omega = states([1, 2, 3, 4])
    saved = {'omega': omega, 'q_ref': [1 / 24] * 24, 'q_marg': [1 / 24] * 24,
             'rows': [{'selection': key(s), 'p_ref': 1 / 6, 'p_marg': 1 / 6, 'p_direct': 1 / 6}
                      for s in matrix(omega, 'quinella')[0]]}
    result = estimate([1, 2, 3, 4], markets, saved, base, options)
    for model in result['estimators'].values():
        np.testing.assert_allclose(model['q'], np.full(24, 1 / 24), atol=1e-7)
    original = copy.deepcopy((result, markets))
    choices = purchases(result, markets['quinella']['quotes'], base, successors, portfolio, options)
    assert len(choices) == 29 and all(c['stake_yen'] == 0 for c in choices.values())
    assert (result, markets) == original


def purchase_inputs(config):
    base, successors, portfolio, options = settings(config)
    selections = ['1-2', '1-3', '2-3']
    estimates = {'selections': selections, 'estimators': {
        'reference': {'probabilities': [0.1, 0.7, 0.2]},
        'alternate': {'probabilities': [0.2, 0.6, 0.2]}}}
    quotes = {s: {'odds': o, 'display_status': 'FIXED'} for s, o in zip(selections, [30, 3, 1.1])}
    return estimates, quotes, base, successors, portfolio, options


def test_hit_choice_differs_from_ev_and_all_objectives_match_joint_outcome_enumeration(config):
    e, quotes, base, successors, portfolio, options = purchase_inputs(config)
    result = purchases(e, quotes, base, successors, portfolio, options)
    assert result['reference:max_ev_single']['tickets'][0]['selection'] == '1-2'
    assert result['reference:max_hit_single']['tickets'][0]['selection'] == '1-3'
    probabilities = np.asarray([x['probabilities'] for x in e['estimators'].values()])
    prices = np.asarray([quotes[s]['odds'] for s in e['selections']])
    oracle = {name: 0 for name in result}
    for size in range(1, 4):
        for subset in combinations(range(3), size):
            amounts = np.zeros(3)
            amounts[list(subset)] = 100
            payoffs = amounts * prices - amounts.sum()
            mean = probabilities @ payoffs
            var = np.asarray([p @ (payoffs - m) ** 2 for p, m in zip(probabilities, mean)])
            profit = probabilities @ (payoffs > 0)
            hit = probabilities @ (amounts > 0)
            for i, model in enumerate(e['estimators']):
                scores = {'max_profit_probability': profit[i],
                          'mean_variance': mean[i] - var[i] / (2 * successors['reference_bankroll_yen'])}
                if size == 1:
                    scores.update(max_ev_single=mean[i], max_hit_single=hit[i])
                if mean[i] > 0:
                    for rule, score in scores.items():
                        label = f'{model}:{rule}'
                        oracle[label] = max(oracle[label], score)
            if mean.min() > 0:
                oracle['all_models:consensus'] = max(oracle['all_models:consensus'], profit.min())
    for label, row in result.items():
        assert row['research_score'] == pytest.approx(oracle[label])
        assert row['evaluated_subsets'] == 7
        assert all(t['stake_yen'] == 100 and t['row']['odds'] == quotes[t['selection']]['odds']
                   for t in row['tickets'])
    # Every model has 80% mass on the first two mutually exclusive winning outcomes.
    hedge = result['all_models:consensus']
    assert hedge['stake_yen'] == 200 and hedge['worst_model_profit_probability'] == pytest.approx(0.8)
    for batch in (1, 2, 100):
        assert purchases(e, quotes, base, successors, {**portfolio, 'enumeration_batch_size': batch}, options) == result
    # With two 100-yen tickets, odds 2 pays only the stake: a hit, not a profit.
    quotes['1-3']['odds'] = 2
    neutral = purchases(e, quotes, base, successors, portfolio, options)['all_models:consensus']
    assert neutral['stake_yen'] == 100 and neutral['worst_model_profit_probability'] == pytest.approx(0.6)


def test_invalid_probabilities_incomplete_prices_and_cash(config):
    e, quotes, base, successors, portfolio, options = purchase_inputs(config)
    for q in quotes.values():
        q['odds'] = 1
    assert all(x['stake_yen'] == 0 for x in purchases(e, quotes, base, successors, portfolio, options).values())
    bad = copy.deepcopy(e)
    bad['estimators']['reference']['probabilities'][0] = 0
    with pytest.raises(ValueError, match='ALTERNATIVE_DISTRIBUTION'):
        purchases(bad, quotes, base, successors, portfolio, options)
    quotes.pop('1-2')
    with pytest.raises(ValueError, match='ALTERNATIVE_INPUT'):
        purchases(e, quotes, base, successors, portfolio, options)


def test_estimators_reject_mismatched_saved_state_order(config):
    base, _, _, options = settings(config)
    markets = fixtures.markets()
    saved = analyze([1, 2, 3, 4], markets, base)
    saved['omega'].reverse()
    with pytest.raises(ValueError, match='ALTERNATIVE_STATE_ORDER'):
        estimate([1, 2, 3, 4], markets, saved, base, options)
