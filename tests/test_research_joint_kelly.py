import copy
from itertools import combinations_with_replacement
import json
from pathlib import Path

import numpy as np
import pytest

from hr_platform.model import key, matrix, states, ticket
from hr_platform.research_joint_kelly import joint_kelly_portfolios


def inputs(base):
    omega = states([1, 2, 3, 4])
    uniform = np.full(len(omega), 1 / len(omega))
    cycle = {(1, 2, 3), (2, 3, 4), (3, 4, 1), (4, 1, 2)}
    reference = np.array([.25 if s in cycle else 0 for s in omega])
    kelly = json.loads(Path('configs/research-kelly.json').read_text())
    config = json.loads(Path('configs/research-joint-kelly.json').read_text())
    config['candidates_per_market'] = 20  # Full oracle search in this small fixture.
    q = {name: {'q': (reference if name == 'reference' else uniform).tolist()} for name in kelly['pool_weights']}
    estimates = {'omega': omega, 'estimators': q}
    markets = {market: {'quotes': {key(s): {'odds': float(2 + i), 'display_status': 'FIXED'}
                                  for i, s in enumerate(matrix(omega, market)[0])}}
               for market in config['markets']}
    successors = json.loads(Path('configs/research-successors.json').read_text())
    portfolio = json.loads(Path('configs/research-portfolio-scenarios.json').read_text())
    return estimates, markets, ['win', 'exacta'], base, successors, portfolio, kelly, config


def test_joint_compressed_optimizer_matches_full_state_payoff_oracle(config):
    args = inputs(config)
    before = copy.deepcopy(args)
    estimates, markets, _, base, successors, portfolio, kelly, options = args
    result = joint_kelly_portfolios(*args)
    assert len(result) == 8
    omega = estimates['omega']
    q = np.array([v['q'] for v in estimates['estimators'].values()])
    pooled = np.array(list(kelly['pool_weights'].values())) @ q
    models = {'reference': q[0], 'same_marginals': q[1], 'pooled': pooled}
    catalog = [(m, s, quote['odds']) for m, market in markets.items() for s, quote in market['quotes'].items()]
    best = {(m, f): 0.0 for m in options['methods'] for f in options['capital_fractions']}
    bankroll = successors['reference_bankroll_yen']
    for size in range(1, 4):
        for indices in combinations_with_replacement(range(len(catalog)), size):
            # Calculate every actual outcome directly; no event matrix, compression,
            # independent Bernoulli approximation or exclusive-only formula.
            net = np.array([sum(100 * catalog[i][2] for i in indices
                                if key(ticket(state, catalog[i][0])) == catalog[i][1]) - 100 * size
                            for state in omega])
            for fraction_name, fraction in options['capital_fractions'].items():
                growth = np.log1p(net / (fraction * bankroll))
                for method in options['methods']:
                    score = min(q @ growth) if method == 'robust' else models[method] @ growth
                    best[method, fraction_name] = max(best[method, fraction_name], score)
    for label, choice in result.items():
        method, fraction = label.removeprefix('joint_kelly_').rsplit('_', 1)
        assert choice['optimization_log_growth'] == pytest.approx(best[method, fraction], abs=base['tie_tolerance'])
        selected = choice['tickets']
        net = np.array([sum(t['stake_yen'] * t['row']['odds'] for t in selected
                            if key(ticket(state, t['market'])) == t['selection']) - choice['stake_yen'] for state in omega])
        model = models.get(method, q[0])
        assert choice['expected_profit_yen'] == pytest.approx(model @ net)
        assert choice['profit_std_yen'] == pytest.approx(np.sqrt(model @ (net - model @ net) ** 2))
        assert choice['worst_model_log_growth_at_reference_bankroll'] == pytest.approx(min(q @ np.log1p(net / bankroll)))
        assert choice['rank_marginal_error'] < base['probability_tolerance']
        assert choice['full_market_counts'] == {'quinella': 6, 'trio': 4}
        assert choice['evaluated_allocations'] == 286
    assert result['joint_kelly_reference_half']['tickets'] != result['joint_kelly_same_marginals_half']['tickets']
    assert args == before


def test_overlapping_tickets_use_joint_probability_not_product_or_exclusivity(config):
    args = inputs(config)
    estimates, markets = args[:2]
    # Both tickets have hit probability .55; their joint .4 exceeds .55 squared.
    q = np.array([{(1, 2, 3): .4, (1, 2, 4): .15, (1, 3, 2): .15, (3, 4, 1): .3}.get(s, 0)
                  for s in estimates['omega']])
    for item in estimates['estimators'].values():
        item['q'] = q.tolist()
    for market in markets.values():
        for quote in market['quotes'].values():
            quote['odds'] = 1
    markets['quinella']['quotes']['1-2']['odds'] = 2.5
    markets['trio']['quotes']['1-2-3']['odds'] = 2.5
    # Equal favorable expectations make a mixed allocation attractive.
    result = joint_kelly_portfolios(*args)
    mixed = [c for c in result.values() if {t['market'] for t in c['tickets']} == {'quinella', 'trio'}]
    assert mixed
    for choice in mixed:
        pair = choice['ticket_dependence'][0]
        assert pair['joint_hit_probability'] == pytest.approx(.4)
        assert pair['hit_indicator_covariance'] == pytest.approx(.4 - .55 ** 2)
        # Joint payoff includes both gross returns; an exclusive formula omits one.
        state = (1, 2, 3)
        gross = sum(t['stake_yen'] * t['row']['odds'] for t in choice['tickets']
                    if key(ticket(state, t['market'])) == t['selection'])
        assert gross > max(t['stake_yen'] * t['row']['odds'] for t in choice['tickets'])


def test_cash_full_quotes_and_same_rank_marginals_are_required(config):
    args = inputs(config)
    for market in args[1].values():
        for quote in market['quotes'].values():
            quote['odds'] = 1
    assert all(c['stake_yen'] == 0 for c in joint_kelly_portfolios(*args).values())
    bad = copy.deepcopy(args)
    bad[1]['trio']['quotes'].pop('1-2-3')
    with pytest.raises(ValueError, match='INCOMPLETE_MARKET'):
        joint_kelly_portfolios(*bad)
    bad = copy.deepcopy(args)
    bad[0]['estimators']['marginal']['q'] = [1] + [0] * 23
    with pytest.raises(ValueError, match='JOINT_KELLY_RANK_MARGINALS'):
        joint_kelly_portfolios(*bad)
    bad = copy.deepcopy(args)
    bad[2].append('trio')
    with pytest.raises(ValueError, match='JOINT_KELLY_CONFIG'):
        joint_kelly_portfolios(*bad)
