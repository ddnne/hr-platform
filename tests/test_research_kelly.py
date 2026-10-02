import copy
from itertools import product
import json
import math
from pathlib import Path

import numpy as np
import pytest

from hr_platform.research_kelly import kelly_portfolios


def inputs(config):
    options = json.loads(Path('configs/research-kelly.json').read_text())
    portfolio = json.loads(Path('configs/research-portfolio-scenarios.json').read_text())
    successors = json.loads(Path('configs/research-successors.json').read_text())
    selections = ['1-2', '1-3', '2-3']
    p = [[.1, .7, .2], [.2, .6, .2], [.15, .55, .3], [.25, .6, .15],
         [.1, .75, .15], [.18, .65, .17], [.13, .67, .2]]
    estimates = {'selections': selections, 'estimators': {
        name: {'probabilities': values} for name, values in zip(options['pool_weights'], p)}}
    quotes = {s: {'odds': o, 'display_status': 'FIXED'} for s, o in zip(selections, [30, 3, 1.1])}
    return estimates, quotes, config, successors, portfolio, options


def test_all_nine_policies_match_independent_exclusive_outcome_payoffs(config):
    e, quotes, base, successors, portfolio, options = inputs(config)
    before = copy.deepcopy((e, quotes, base, successors, portfolio, options))
    result = kelly_portfolios(e, quotes, base, successors, portfolio, options)
    assert len(result) == 9
    p = np.asarray([e['estimators'][name]['probabilities'] for name in options['pool_weights']])
    pooled = np.asarray(list(options['pool_weights'].values())) @ p
    prices = np.asarray([quotes[s]['odds'] for s in e['selections']])
    bankroll = successors['reference_bankroll_yen']
    for label, choice in result.items():
        _, method, fraction = label.split('_')
        capital = bankroll * options['capital_fractions'][fraction]
        best = 0
        for amounts in product(range(0, portfolio['race_budget_yen']+1, portfolio['stake_unit_yen']), repeat=3):
            if sum(amounts) > portfolio['race_budget_yen']:
                continue
            payoff = np.asarray(amounts) * prices - sum(amounts)
            gains = np.asarray([math.log((capital + net) / capital) for net in payoff])
            score = min(p @ gains) if method == 'robust' else (pooled if method == 'pooled' else p[0]) @ gains
            best = max(best, score)
        assert choice['optimization_log_growth'] == pytest.approx(best, abs=base['tie_tolerance'])
        amounts = np.asarray([sum(t['stake_yen'] for t in choice['tickets'] if t['selection'] == s)
                              for s in e['selections']])
        net = amounts * prices - amounts.sum()
        actual_growth = np.log1p(net / bankroll)
        model = pooled if method == 'pooled' else p[0]
        assert choice['expected_profit_yen'] == pytest.approx(model @ net)
        assert choice['expected_log_growth_at_reference_bankroll'] == pytest.approx(model @ actual_growth)
        assert choice['worst_model_log_growth_at_reference_bankroll'] == pytest.approx(min(p @ actual_growth))
        assert choice['hit_probability'] == pytest.approx(model @ (amounts > 0))
        assert choice['profit_probability'] == pytest.approx(model @ (net > 0))
    assert (e, quotes, base, successors, portfolio, options) == before


def test_fractional_capital_matches_analytic_single_bet_kelly_before_rounding(config):
    e, quotes, base, successors, portfolio, options = inputs(config)
    for model in e['estimators'].values():
        model['probabilities'] = [.39, .30, .31]
    for selection, odds in zip(e['selections'], [3, 1, 1]):
        quotes[selection]['odds'] = odds
    portfolio['race_budget_yen'] = 900
    result = kelly_portfolios(e, quotes, base, successors, portfolio, options)
    # The sole favorable bet has continuous Kelly fraction (p*odds-1)/(odds-1).
    continuous = (.39 * 3 - 1) / (3 - 1)
    for name, fraction in options['capital_fractions'].items():
        capital = successors['reference_bankroll_yen'] * fraction
        center = continuous * capital
        neighbors = {100 * math.floor(center / 100), 100 * math.ceil(center / 100)}
        def gain(stake):
            return .39 * math.log1p(stake * 2 / capital) + .61 * math.log1p(-stake / capital)
        expected = max(neighbors, key=gain)
        for method in options['methods']:
            c = result[f'kelly_{method}_{name}']
            assert c['stake_yen'] == expected
            assert len(c['tickets']) == 1 and c['tickets'][0]['selection'] == '1-2'
    assert result['kelly_reference_full']['stake_yen'] > result['kelly_reference_half']['stake_yen']
    assert result['kelly_reference_half']['stake_yen'] > result['kelly_reference_quarter']['stake_yen']


def test_worst_model_kelly_can_hold_a_joint_hedge_when_each_single_bet_is_unfavorable(config):
    e, quotes, base, successors, portfolio, options = inputs(config)
    for i, model in enumerate(e['estimators'].values()):
        model['probabilities'] = [.75, .2, .05] if i % 2 else [.2, .75, .05]
    for selection, odds in zip(e['selections'], [2.5, 2.5, 1.1]):
        quotes[selection]['odds'] = odds
    c = kelly_portfolios(e, quotes, base, successors, portfolio, options)['kelly_robust_full']
    assert c['stake_yen'] == 200
    assert {t['selection'] for t in c['tickets']} == {'1-2', '1-3'}
    assert c['worst_model_expected_profit_yen'] == pytest.approx(37.5)
    assert c['worst_model_log_growth_at_reference_bankroll'] > 0


def test_cash_incomplete_support_and_invalid_probability_or_capital(config):
    args = inputs(config)
    for quote in args[1].values():
        quote['odds'] = 1
    assert all(c['stake_yen'] == 0 for c in kelly_portfolios(*args).values())
    bad = copy.deepcopy(args)
    bad[0]['estimators']['reference']['probabilities'][0] = 0
    with pytest.raises(ValueError, match='ALTERNATIVE_DISTRIBUTION'):
        kelly_portfolios(*bad)
    bad = copy.deepcopy(args)
    bad[1].pop('1-2')
    with pytest.raises(ValueError, match='KELLY_CONFIG_OR_SUPPORT'):
        kelly_portfolios(*bad)
    for fraction in (0, -1, 2, float('nan'), .01):
        bad = copy.deepcopy(args)
        bad[5]['capital_fractions']['half'] = fraction
        with pytest.raises(ValueError, match='KELLY_CONFIG_OR_SUPPORT'):
            kelly_portfolios(*bad)
