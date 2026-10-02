"""Mixed ticket Kelly from full top-three outcomes, including overlapping wins."""
from itertools import combinations_with_replacement, islice
import math

import numpy as np

from .model import key, marginals, reference, states
from .research_alternatives import _distribution


def joint_kelly_portfolios(estimates, markets, calibration_markets, base, successors,
                           portfolio, kelly, config):
    """Use all prices to screen a common candidate set, then enumerate its stakes.

    State compression is exact. Candidate screening is an approximation; it does
    not find the global optimum over every quoted ticket. Both purchased markets
    must be excluded from calibration. Same-marginal Q changes rank dependence,
    which can change individual ticket probabilities as well as their covariance.
    """
    omega = [tuple(s) for s in estimates['omega']]
    horses = sorted({h for s in omega for h in s})
    weights, fractions = kelly['pool_weights'], config['capital_fractions']
    names = list(weights)
    unit, budget, maximum, batch = (portfolio[k] for k in
        ('stake_unit_yen', 'race_budget_yen', 'max_tickets_per_race', 'enumeration_batch_size'))
    bankroll = successors['reference_bankroll_yen']
    tolerance, tie = base['probability_tolerance'], base['tie_tolerance']
    cap = config['candidates_per_market']
    if (omega != states(horses) or config['markets'] != ['quinella', 'trio']
            or config['methods'] != ['reference', 'same_marginals', 'pooled', 'robust']
            or config['candidate_rule'] != 'best_single_unit_log_growth_across_models_and_capital_fractions'
            or config['objective'] != 'joint_top3_state_log_wealth_including_cash'
            or set(calibration_markets) != {'win', 'exacta'}
            or set(names) != set(estimates['estimators']) or not {'reference', 'marginal'} <= set(names)
            or any(type(w) not in (int, float) or not math.isfinite(w) or w < 0 for w in weights.values())
            or abs(sum(weights.values()) - 1) > tolerance
            or any(type(v) is not int or v <= 0 for v in (unit, budget, maximum, batch, cap))
            or unit != base['stake_yen'] or budget % unit or not fractions
            or type(bankroll) not in (int, float) or not math.isfinite(bankroll)
            or any(type(a) not in (int, float) or not math.isfinite(a) or not 0 < a <= 1
                   or not budget < a * bankroll for a in fractions.values())):
        raise ValueError('JOINT_KELLY_CONFIG')
    q = np.asarray([_distribution(estimates['estimators'][name]['q'], len(omega), tolerance) for name in names])
    ref_index, marginal_index = names.index('reference'), names.index('marginal')
    rank_error = float(np.max(np.abs(marginals(omega) @ (q[ref_index] - q[marginal_index]))))
    if rank_error > tolerance:
        raise ValueError('JOINT_KELLY_RANK_MARGINALS')
    events, prices, catalog = [], [], []
    full_counts = {}
    for market in config['markets']:
        selections, event, _, _, odds = reference(omega, market, markets[market]['quotes'])
        probabilities = np.asarray(event @ q.T).T
        # Screen independently of subsequent allocation method, allowing negative
        # standalone candidates too: a joint worst-scenario hedge may still help.
        score = np.full(len(selections), -np.inf)
        for fraction in fractions.values():
            capital = fraction * bankroll
            lose, win = np.log1p(-unit / capital), np.log1p(unit * (odds - 1) / capital)
            score = np.maximum(score, (lose + probabilities * (win - lose)).max(axis=0))
        selected = sorted(sorted(range(len(selections)), key=lambda i: (-score[i], selections[i]))[:cap])
        full_counts[market] = len(selections)
        events.append(event[selected].toarray().T.astype(bool))
        prices.extend(odds[selected])
        catalog.extend({'market': market, 'selection': key(selections[i])} for i in selected)
    indicators = np.concatenate(events, axis=1)
    prices = np.asarray(prices)
    patterns, inverse = np.unique(indicators, axis=0, return_inverse=True)
    compressed_q = np.asarray([np.bincount(inverse, weights=row, minlength=len(patterns)) for row in q])
    pooled = np.asarray(list(weights.values())) @ compressed_q
    models = {'reference': compressed_q[ref_index], 'same_marginals': compressed_q[marginal_index],
              'pooled': pooled, 'robust': compressed_q[ref_index]}
    best = {(m, f): (0.0, ()) for m in config['methods'] for f in fractions}
    evaluated = 1  # Cash.
    for total_units in range(1, budget // unit + 1):
        allocations = combinations_with_replacement(range(len(catalog)), total_units)
        while block := list(islice(allocations, batch)):
            block = [selection for selection in block if len(set(selection)) <= maximum]
            if not block:
                continue
            amounts = unit * np.asarray([np.bincount(selection, minlength=len(catalog)) for selection in block])
            payoff = np.einsum('oi,bi->ob', patterns, amounts * prices) - amounts.sum(axis=1)
            evaluated += len(block)
            for fraction_name, fraction in fractions.items():
                log_growth = np.log1p(payoff / (fraction * bankroll))
                scores = {name: np.einsum('o,ob->b', model, log_growth) for name, model in models.items()}
                scores['robust'] = np.einsum('mo,ob->mb', compressed_q, log_growth).min(axis=0)
                for method, values in scores.items():
                    # Iteration order gives smaller stakes, then canonical tickets,
                    # when objective values differ by at most the configured tie.
                    for i in np.flatnonzero(values > best[method, fraction_name][0] + tie):
                        if values[i] > best[method, fraction_name][0] + tie:
                            best[method, fraction_name] = (float(values[i]), block[i])
    result = {}
    for (method, fraction_name), (score, selection) in best.items():
        amounts = unit * np.bincount(selection, minlength=len(catalog))
        chosen = np.flatnonzero(amounts)
        stake = int(amounts.sum())
        payoff = patterns @ (amounts * prices) - stake
        growth = np.log1p(payoff / bankroll)
        model = models[method]
        mean = float(model @ payoff)
        dependence = []
        for pos, i in enumerate(chosen):
            for j in chosen[pos + 1:]:
                joint = float(model @ (patterns[:, i] & patterns[:, j]))
                independent = float((model @ patterns[:, i]) * (model @ patterns[:, j]))
                dependence.append({'left': catalog[i], 'right': catalog[j], 'joint_hit_probability': joint,
                                   'product_of_hit_probabilities': independent,
                                   'hit_indicator_covariance': joint - independent})
        result[f'joint_kelly_{method}_{fraction_name}'] = {
            'status': 'CANDIDATE' if stake else 'NO_EDGE', 'stake_yen': stake,
            'tickets': [{**catalog[i], 'stake_yen': int(amounts[i]),
                         'row': {**catalog[i], 'odds': float(prices[i])}} for i in chosen],
            'capital_fraction': fractions[fraction_name], 'reference_bankroll_yen': bankroll,
            'optimization_bankroll_yen': fractions[fraction_name] * bankroll,
            'optimization_log_growth': score,
            'expected_log_growth_at_reference_bankroll': float(model @ growth),
            'worst_model_log_growth_at_reference_bankroll': float((compressed_q @ growth).min()),
            'expected_profit_yen': mean, 'worst_model_expected_profit_yen': float((compressed_q @ payoff).min()),
            'hit_probability': float(model @ (patterns[:, chosen].any(axis=1))),
            'profit_probability': float(model @ (payoff > 0)),
            'profit_std_yen': float(np.sqrt(model @ ((payoff - mean) ** 2))),
            'same_allocation_expected_profit_reference_yen': float(compressed_q[ref_index] @ payoff),
            'same_allocation_expected_profit_same_marginals_yen': float(compressed_q[marginal_index] @ payoff),
            'diagnostic_distribution': 'reference' if method == 'robust' else method,
            'ticket_dependence': dependence, 'rank_marginal_error': rank_error,
            'full_market_counts': full_counts, 'screened_candidates': len(catalog),
            'compressed_outcomes': len(patterns), 'evaluated_allocations': evaluated,
        }
    return result
