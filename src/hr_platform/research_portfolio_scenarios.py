"""Evaluate all quinella allocations under exclusive, normalized outcome scenarios."""
from collections import Counter
from itertools import combinations_with_replacement, islice
import math

import numpy as np


def scenario_portfolios(analysis, base, successors_config, config):
    """Maximize log wealth on saved distributions; never treat tickets as independent."""
    rows = analysis['rows']
    fields, families = config['probability_fields'], config['sensitivity_families']
    probabilities = np.asarray([[r[field] for r in rows] for field in fields]
                               + [v['probabilities'] for family in families for v in analysis[family]], dtype=float)
    odds = np.asarray([r['odds'] for r in rows], dtype=float)
    if (not rows or fields[0] != 'p_ref' or probabilities.ndim != 2
            or probabilities.shape[1] != len(rows) or len({r['selection'] for r in rows}) != len(rows)
            or not np.isfinite(probabilities).all() or (probabilities < 0).any() or (probabilities > 1).any()
            or (np.abs(probabilities.sum(axis=1) - 1) > base['probability_tolerance']).any()
            or not np.isfinite(odds).all() or (odds < 1).any()):
        raise ValueError('PORTFOLIO_SCENARIOS')
    unit, budget, limit, batch_size = (config[k] for k in
        ('stake_unit_yen', 'race_budget_yen', 'max_tickets_per_race', 'enumeration_batch_size'))
    bankroll = successors_config['reference_bankroll_yen']
    if (any(type(v) is not int or v <= 0 for v in (unit, budget, limit, batch_size))
            or unit != base['stake_yen'] or budget % unit or not 0 < budget < bankroll
            or config['target'] != 'quinella'
            or set(config['variants']) != {'reference_joint', 'robust_joint'}):
        raise ValueError('PORTFOLIO_SCENARIO_CONFIG')
    frontier = {variant: [] for variant in config['variants']}
    maxima = dict.fromkeys(config['variants'], 0.0)
    evaluated = 0

    def tickets_for(indices):
        return sorted(({'selection': rows[i]['selection'], 'stake_yen': n * unit, 'row': rows[i]}
                       for i, n in Counter(indices).items()), key=lambda t: t['selection'])

    for units in range(1, budget // unit + 1):
        combinations = combinations_with_replacement(range(len(rows)), units)
        cash = 1 - units * unit / bankroll
        while block := list(islice(combinations, batch_size)):
            indices = np.asarray(block)
            unique = np.ones_like(indices, dtype=bool)
            unique[:, 1:] = indices[:, 1:] != indices[:, :-1]
            valid = unique.sum(axis=1) <= limit
            indices, unique = indices[valid], unique[valid]
            if not len(indices):
                continue
            evaluated += len(indices)
            gains = np.full((len(probabilities), len(indices)), math.log(cash))
            for position in range(units):
                selected = indices[:, position]
                counts = (indices == selected[:, None]).sum(axis=1)
                increment = np.log1p(unit * counts * odds[selected] / bankroll / cash)
                gains += probabilities[:, selected] * (increment * unique[:, position])
            for variant, scores in (('reference_joint', gains[0]), ('robust_joint', gains.min(axis=0))):
                maxima[variant] = max(maxima[variant], float(scores.max()))
                threshold = maxima[variant] - base['tie_tolerance']
                choices = [old for old in frontier[variant] if old['research_score'] >= threshold]
                for index in np.flatnonzero((scores > 0) & (scores >= threshold)):
                    tickets = tickets_for(indices[index].tolist())
                    choices.append({'tickets': tickets, 'research_score': float(scores[index])})
                choices.sort(key=lambda choice: [(t['selection'], t['stake_yen']) for t in choice['tickets']])
                # Retain score/lexicographic tradeoffs until the final maximum is known.
                # A slightly better later batch may invalidate an earlier lexicographic winner.
                frontier[variant], highest = [], 0.0
                for choice in choices:
                    if choice['research_score'] > highest:
                        frontier[variant].append(choice)
                        highest = choice['research_score']

    best = {}
    for variant, choices in frontier.items():
        choice = choices[0] if choices else {'tickets': [], 'research_score': 0.0}
        stake = sum(t['stake_yen'] for t in choice['tickets'])
        amounts = np.zeros(len(rows))
        by_selection = {r['selection']: i for i, r in enumerate(rows)}
        for ticket in choice['tickets']:
            amounts[by_selection[ticket['selection']]] = ticket['stake_yen']
        payoffs = amounts * odds - stake
        expected = probabilities @ payoffs
        hit = probabilities @ (amounts > 0)
        profit = probabilities @ (payoffs > 0)
        best[variant] = {**choice, 'stake_yen': stake, 'candidate_count': len(rows),
                         'evaluated_allocations': evaluated, 'scenario_count': len(probabilities),
                         'expected_profit_yen': float(expected[0]),
                         'worst_scenario_expected_profit_yen': float(expected.min()),
                         'hit_probability': float(hit[0]), 'worst_scenario_hit_probability': float(hit.min()),
                         'profit_probability': float(profit[0]), 'worst_scenario_profit_probability': float(profit.min()),
                         'profit_std_yen': float(np.sqrt(probabilities[0] @ ((payoffs - expected[0]) ** 2)))}
    return best
