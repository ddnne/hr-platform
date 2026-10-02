"""Additional fixed hypotheses using existing fits and portfolio selectors."""
import math

import numpy as np

from .paper_rules import select
from .research_portfolio import growth
from .research_portfolio_scenarios import scenario_portfolios


def distributions(analysis, base, portfolio_config):
    rows = analysis['rows']
    p = np.asarray([[r[f] for r in rows] for f in portfolio_config['probability_fields']]
                   + [v['probabilities'] for family in portfolio_config['sensitivity_families']
                      for v in analysis[family]], dtype=float)
    if (not rows or p.ndim != 2 or p.shape[1] != len(rows) or not np.isfinite(p).all()
            or (p < 0).any() or (p > 1).any()
            or (np.abs(p.sum(axis=1) - 1) > base['probability_tolerance']).any()
            or len({r['selection'] for r in rows}) != len(rows)
            or any(not math.isfinite(r['odds']) or r['odds'] < 1 for r in rows)
            or portfolio_config['probability_fields'][0] != 'p_ref'):
        raise ValueError('EXPANSION_DISTRIBUTIONS')
    return p


def conditional_portfolio(analysis, base, successors_config, portfolio_config, *, robust=False):
    """Choose backups before the race; never assume a failure has been observed."""
    rows = analysis['rows']
    p = distributions(analysis, base, portfolio_config)
    bankroll = successors_config['reference_bankroll_yen']
    unit, budget, limit = (portfolio_config[k] for k in
                           ('stake_unit_yen', 'race_budget_yen', 'max_tickets_per_race'))
    if (portfolio_config['target'] != 'quinella' or unit != base['stake_yen']
            or any(type(v) is not int or v <= 0 for v in (unit, budget, limit))
            or budget % unit or not budget < bankroll):
        raise ValueError('CONDITIONAL_PORTFOLIO_CONFIG')
    anchor = select(rows, 'reference', base['tie_tolerance'])
    indices, steps = [], []

    def tickets(selected, probabilities):
        return [{'selection': rows[i]['selection'], 'stake_yen': unit,
                 'row': {**rows[i], 'p_ref': float(probabilities[i])}} for i in selected]

    def scores(selected):
        return np.asarray([growth(tickets(selected, q), bankroll) for q in p])

    def objective(values):
        return float(values.min() if robust else values[0])

    if anchor:
        indices = [next(i for i, r in enumerate(rows) if r['selection'] == anchor['selection'])]
        current = objective(scores(indices))
        steps.append({'selection': anchor['selection'], 'stage': 'ANCHOR', 'portfolio_growth': current})
        while len(indices) < min(limit, budget // unit):
            failure = 1 - p[:, indices].sum(axis=1)
            if (failure <= base['probability_tolerance']).any():
                break
            conditional = p / failure[:, None]
            conditional[:, indices] = 0
            choices = []
            for i, row in enumerate(rows):
                if i in indices:
                    continue
                branch = objective(np.asarray([growth(tickets([i], q), bankroll - unit * len(indices))
                                               for q in conditional]))
                total = objective(scores([*indices, i]))
                if branch > 0 and total > current + base['tie_tolerance']:
                    choices.append((branch, row['selection'], i, total))
            if not choices:
                break
            maximum = max(c[0] for c in choices)
            branch, _, i, current = min((c for c in choices if maximum - c[0] <= base['tie_tolerance']),
                                       key=lambda c: c[1])
            steps.append({'selection': rows[i]['selection'], 'stage': 'BACKUP',
                          'prior_failure_probability': failure.tolist(),
                          'conditional_probability': conditional[:, i].tolist(),
                          'conditional_growth': branch, 'portfolio_growth': current})
            indices.append(i)
        if current <= 0:
            return {'status': 'NO_EDGE', 'tickets': [], 'stake_yen': 0, 'research_score': 0.0,
                    'proposal': {'steps': steps, 'research_score': current}}
    final = sorted(({'selection': rows[i]['selection'], 'stake_yen': unit, 'row': rows[i]}
                    for i in indices), key=lambda t: t['selection'])
    amounts = np.asarray([unit if i in indices else 0 for i in range(len(rows))])
    payoffs = amounts * np.asarray([r['odds'] for r in rows]) - amounts.sum()
    expected, hit, profitable = p @ payoffs, p @ (amounts > 0), p @ (payoffs > 0)
    return {'status': 'CANDIDATE' if final else 'NO_EDGE', 'tickets': final,
            'stake_yen': int(amounts.sum()), 'research_score': objective(scores(indices)) if indices else 0.0,
            'steps': steps, 'expected_profit_yen': float(expected[0]),
            'worst_scenario_expected_profit_yen': float(expected.min()),
            'hit_probability': float(hit[0]), 'worst_scenario_hit_probability': float(hit.min()),
            'profit_probability': float(profitable[0]), 'worst_scenario_profit_probability': float(profitable.min()),
            'profit_std_yen': float(np.sqrt(p[0] @ ((payoffs - expected[0]) ** 2)))}


def fixed_distribution_portfolio(analysis, probabilities, odds, base, successors_config, portfolio_config):
    """Use the existing exhaustive allocation search; retain observed ticket prices."""
    rows = analysis['rows']
    transformed = {'rows': [{**r, 'p_ref': float(p), 'odds': float(o)}
                            for r, p, o in zip(rows, probabilities, odds)]}
    result = scenario_portfolios(transformed, base, successors_config,
                                {**portfolio_config, 'probability_fields': ['p_ref'], 'sensitivity_families': []})['reference_joint']
    original = {r['selection']: r for r in rows}
    result['tickets'] = [{**t, 'research_projected_odds': t['row']['odds'], 'row': original[t['selection']]}
                         for t in result['tickets']]
    return {**result, 'status': 'CANDIDATE' if result['stake_yen'] else 'NO_EDGE'}


def expansions(analysis, base, successors_config, portfolio_config, config, *, previous_quotes=None, elapsed_seconds=None):
    p = distributions(analysis, base, portfolio_config)
    rows = analysis['rows']
    if (set(config['variants']) != {'conditional_reference', 'conditional_robust', 'dependence_half',
                                  'dependence_marginal', 'trend_damped_joint', 'trend_adverse_joint'}
            or config['conditional_anchor'] != 'reference_baseline'
            or config['conditional_acceptance'] != 'positive_conditional_growth_and_improved_unconditional_portfolio_growth'
            or not 0 <= config['trend_damping'] <= 1
            or not 0 < config['trend_factor_min'] <= 1 <= config['trend_factor_max']
            or not 0 < config['trend_max_extrapolation_multiple'] <= 1 or config['trend_horizon_seconds'] <= 0):
        raise ValueError('EXPANSION_CONFIG')
    result = {name: conditional_portfolio(analysis, base, successors_config, portfolio_config,
                                         robust=name == 'conditional_robust')
              for name in ('conditional_reference', 'conditional_robust')}
    odds = np.asarray([r['odds'] for r in rows])
    marginal = np.asarray([r['p_marg'] for r in rows])
    for name in ('dependence_half', 'dependence_marginal'):
        weight = config['dependence_reference_weights'][name]
        if not 0 <= weight <= 1:
            raise ValueError('DEPENDENCE_WEIGHT')
        mixed = weight * p[0] + (1 - weight) * marginal
        result[name] = fixed_distribution_portfolio(analysis, mixed, odds, base, successors_config, portfolio_config)
    trend_names = ('trend_damped_joint', 'trend_adverse_joint')
    if previous_quotes is None:
        result.update({name: {'status': 'INPUT_EXCLUDED', 'reason': 'PREVIOUS_OBSERVATION_MISSING',
                              'tickets': [], 'stake_yen': 0} for name in trend_names})
    else:
        if (set(previous_quotes) != {r['selection'] for r in rows} or elapsed_seconds is None
                or not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0
                or any(not math.isfinite(q) or q < 1 for q in previous_quotes.values())):
            raise ValueError('TREND_INPUT')
        exponent = config['trend_damping'] * min(config['trend_horizon_seconds'] / elapsed_seconds,
                                               config['trend_max_extrapolation_multiple'])
        factors = np.clip(np.exp(exponent * np.log(odds / np.asarray([previous_quotes[r['selection']] for r in rows]))),
                          config['trend_factor_min'], config['trend_factor_max'])
        for name, factor in zip(trend_names, (factors, np.minimum(1, factors))):
            projected = np.maximum(1, odds * factor)
            result[name] = {**fixed_distribution_portfolio(analysis, p[0], projected, base, successors_config, portfolio_config),
                            'trend_elapsed_seconds': elapsed_seconds, 'trend_projection_exponent': exponent}
    return result
