"""Constrained Kelly allocations on joint quinella outcomes, with cash allowed."""
import math

import numpy as np

from .model import key, reference, states
from .research_alternatives import _distribution
from .research_portfolio_scenarios import scenario_portfolios


def kelly_portfolios(estimates, quotes, base, successors, portfolio, config):
    """Optimize using alpha times the reference bankroll, keeping unit/budget caps.

    Without discrete stakes or binding constraints this is fractional Kelly.
    Under these constraints it is a direct optimization on a reduced bankroll,
    not scaling an already capped allocation. Outcomes are mutually exclusive;
    probabilities are uncalibrated market-implied model scenarios.
    """
    selections = estimates['selections']
    weights = config['pool_weights']
    names = list(weights)
    fractions = config['capital_fractions']
    bankroll = successors['reference_bankroll_yen']
    tolerance = base['probability_tolerance']
    if (config['target'] != 'quinella' or base['target'] != 'quinella'
            or config['methods'] != ['reference', 'pooled', 'robust']
            or config['fractional_rule'] != 'optimize_log_wealth_on_fraction_of_reference_bankroll_with_integer_stakes_and_same_limits'
            or config['bankroll_rule'] != 'fixed_reference_bankroll_no_outcome_compounding'
            or 'reference' not in weights or set(names) != set(estimates['estimators'])
            or any(type(w) not in (int, float) or not math.isfinite(w) or w < 0 for w in weights.values())
            or abs(sum(weights.values()) - 1) > tolerance or not fractions
            or any(type(a) not in (int, float) or not math.isfinite(a) or not 0 < a <= 1
                   or not portfolio['race_budget_yen'] < a * bankroll for a in fractions.values())
            or not selections or len(set(selections)) != len(selections) or set(quotes) != set(selections)):
        raise ValueError('KELLY_CONFIG_OR_SUPPORT')
    p = np.asarray([_distribution(estimates['estimators'][name]['probabilities'], len(selections), tolerance)
                    for name in names])
    omega = states(sorted({int(h) for s in selections for h in s.split('-')}))
    ordered, _, _, _, odds = reference(omega, 'quinella', quotes)
    if [key(s) for s in ordered] != selections:
        raise ValueError('KELLY_SELECTION_ORDER')
    pooled = np.asarray(list(weights.values())) @ p
    pref = p[names.index('reference')]
    fields = [f'scenario_{i}' for i in range(len(names))]
    rows = [{'selection': s, 'odds': float(odds[j]), 'p_ref': float(pref[j]),
             **{field: float(p[i, j]) for i, field in enumerate(fields)}} for j, s in enumerate(selections)]
    options = {**portfolio, 'probability_fields': ['p_ref', *fields], 'sensitivity_families': []}
    pooled_rows = [{**r, 'p_ref': float(pooled[j])} for j, r in enumerate(rows)]
    result = {}
    for fraction_name, fraction in fractions.items():
        allocation_config = {**successors, 'reference_bankroll_yen': fraction * bankroll}
        choices = scenario_portfolios({'rows': rows}, base, allocation_config, options)
        combined = scenario_portfolios({'rows': pooled_rows}, base, allocation_config,
                                      {**options, 'probability_fields': ['p_ref']})['reference_joint']
        for method, choice in [('reference', choices['reference_joint']), ('pooled', combined),
                               ('robust', choices['robust_joint'])]:
            amounts = np.zeros(len(selections))
            for ticket in choice['tickets']:
                amounts[selections.index(ticket['selection'])] = ticket['stake_yen']
            stake = int(amounts.sum())
            payoffs = amounts * odds - stake
            log_growth = np.log1p(payoffs / bankroll)
            model = pooled if method == 'pooled' else pref
            expected = float(model @ payoffs)
            result[f'kelly_{method}_{fraction_name}'] = {
                'status': 'CANDIDATE' if stake else 'NO_EDGE', 'stake_yen': stake,
                'tickets': [{'selection': t['selection'], 'stake_yen': t['stake_yen'],
                             'row': {'selection': t['selection'], 'odds': t['row']['odds']}}
                            for t in choice['tickets']],
                'capital_fraction': fraction, 'reference_bankroll_yen': bankroll,
                'optimization_bankroll_yen': fraction * bankroll,
                'optimization_log_growth': choice['research_score'],
                'expected_log_growth_at_reference_bankroll': float(model @ log_growth),
                'worst_model_log_growth_at_reference_bankroll': float((p @ log_growth).min()),
                'expected_profit_yen': expected, 'worst_model_expected_profit_yen': float((p @ payoffs).min()),
                'hit_probability': float(model @ (amounts > 0)),
                'profit_probability': float(model @ (payoffs > 0)),
                'profit_std_yen': float(np.sqrt(model @ ((payoffs - expected) ** 2))),
                'evaluated_allocations': choice['evaluated_allocations'],
            }
    return result
