"""Full top-three distributions and fixed purchase rules, using saved inputs only."""
from itertools import combinations, islice
import math

import numpy as np

from .model import key, matrix, reference, states


def _distribution(q, size, tolerance):
    q = np.asarray(q, dtype=float)
    if (q.shape != (size,) or not np.isfinite(q).all() or (q < 0).any()
            or abs(q.sum() - 1) > tolerance):
        raise ValueError('ALTERNATIVE_DISTRIBUTION')
    return q


def estimate(runners, markets, saved_analysis, base, config):
    """Target prices never enter estimation. PL is a comparison, not the main model."""
    if base['target'] != 'quinella' or config['target'] != base['target']:
        raise ValueError('ALTERNATIVE_TARGET')
    power = config['exacta_power']
    if type(power) not in (int, float) or not math.isfinite(power) or power <= 0:
        raise ValueError('ALTERNATIVE_POWER')
    omega = states(runners)
    if [list(s) for s in omega] != [list(s) for s in saved_analysis['omega']]:
        raise ValueError('ALTERNATIVE_STATE_ORDER')
    tolerance = base['probability_tolerance']
    win_s, _, win_p, _, _ = reference(omega, 'win', markets['win']['quotes'])
    pair_s, _, pair_p, _, _ = reference(omega, 'exacta', markets['exacta']['quotes'])
    win, pair = dict(zip(win_s, win_p)), dict(zip(pair_s, pair_p))
    row_sum = {i: sum(p for (a, _), p in pair.items() if a == i) for i in runners}
    powered = pair_p ** power
    powered /= powered.sum()
    powered = dict(zip(pair_s, powered))
    remaining = {(i, j): sum(win[(k,)] for k in runners if k not in (i, j)) for i, j in pair}
    q = {name: _distribution(saved_analysis[field], len(omega), tolerance)
         for name, field in [('reference', 'q_ref'), ('marginal', 'q_marg')]}
    q['direct'] = np.asarray([pair[(i, j)] / (len(runners) - 2) for i, j, k in omega])
    q['win_pl'] = np.asarray([win[(i,)] * win[(j,)] / sum(win[(h,)] for h in runners if h != i)
                             * win[(k,)] / remaining[(i, j)] for i, j, k in omega])
    q['win_exacta_chain'] = np.asarray([win[(i,)] * pair[(i, j)] / row_sum[i]
                                      * win[(k,)] / remaining[(i, j)] for i, j, k in omega])
    q['exacta_power'] = np.asarray([powered[(i, j)] * win[(k,)] / remaining[(i, j)]
                                   for i, j, k in omega])
    weights = config['ensemble_weights']
    if (set(weights) != {'reference', 'win_pl', 'win_exacta_chain', 'exacta_power'}
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values())
            or abs(sum(weights.values()) - 1) > tolerance):
        raise ValueError('ALTERNATIVE_ENSEMBLE')
    q['ensemble'] = sum(weight * q[name] for name, weight in weights.items())
    selections, a = matrix(omega, base['target'])
    result = {}
    for name, values in q.items():
        values = _distribution(values, len(omega), tolerance)
        result[name] = {'q': values.tolist(), 'probabilities': (a @ values).tolist()}
    by_ticket = {r['selection']: r for r in saved_analysis['rows']}
    for name, field in [('reference', 'p_ref'), ('marginal', 'p_marg'), ('direct', 'p_direct')]:
        if any(abs(p - by_ticket[key(s)][field]) > tolerance
               for s, p in zip(selections, result[name]['probabilities'])):
            raise ValueError('ALTERNATIVE_SAVED_FIT_MISMATCH')
    return {'omega': omega, 'selections': [key(s) for s in selections], 'estimators': result,
            'basis': 'MARKET_IMPLIED_DISTRIBUTIONS_NOT_VERIFIED_WIN_PROBABILITIES'}


def purchases(estimates, quotes, base, successors, portfolio, config):
    """Enumerate distinct equal-stake tickets once for every model and objective.

    Ordinary quinella outcomes are exclusive. Means, variances and probabilities
    below use that joint payoff, not independent Bernoulli ticket returns.
    """
    selections = estimates['selections']
    names = list(estimates['estimators'])
    if (not names or len(set(selections)) != len(selections) or not selections
            or set(quotes) != set(selections)):
        raise ValueError('ALTERNATIVE_INPUT')
    p = np.asarray([_distribution(estimates['estimators'][name]['probabilities'], len(selections),
                                 base['probability_tolerance']) for name in names])
    omega = states(sorted({int(h) for s in selections for h in s.split('-')}))
    ordered, _, _, _, prices = reference(omega, config['target'], quotes)
    if [key(s) for s in ordered] != selections:
        raise ValueError('ALTERNATIVE_SELECTION_ORDER')
    unit, budget, maximum, batch = (portfolio[k] for k in
        ('stake_unit_yen', 'race_budget_yen', 'max_tickets_per_race', 'enumeration_batch_size'))
    bankroll = successors['reference_bankroll_yen']
    aversion, minimum = config['risk_aversion'], config['minimum_expected_profit_yen']
    policies = ['max_ev_single', 'max_hit_single', 'max_profit_probability', 'mean_variance']
    if (config['target'] != 'quinella' or base['target'] != 'quinella' or portfolio['target'] != 'quinella'
            or config['policies'] != policies or config['consensus_policy'] != 'worst_model_profit_probability'
            or config['allocation_rule'] != 'one_stake_unit_per_selection_all_subsets_up_to_existing_budget'
            or any(type(v) is not int or v <= 0 for v in (unit, budget, maximum, batch))
            or unit != base['stake_yen'] or budget % unit or not 0 < budget < bankroll
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (aversion, minimum))):
        raise ValueError('ALTERNATIVE_PURCHASE_CONFIG')
    best = {f'{name}:{policy}': None for name in names for policy in policies}
    best['all_models:consensus'] = None
    count = 0

    def keep(label, scores, admissible, indices):
        scores = np.where(admissible & (scores > 0), scores, -np.inf)
        pos = int(np.argmax(scores))
        if not np.isfinite(scores[pos]):
            return
        proposal = (float(scores[pos]), tuple(indices[pos].tolist()))
        old = best[label]
        if (old is None or proposal[0] > old[0]
                or proposal[0] == old[0] and (len(proposal[1]), proposal[1]) < (len(old[1]), old[1])):
            best[label] = proposal

    for size in range(1, min(maximum, budget // unit, len(selections)) + 1):
        subsets = combinations(range(len(selections)), size)
        while block := list(islice(subsets, batch)):
            indices = np.asarray(block)
            count += len(indices)
            payout = unit * prices[indices]
            total = unit * size
            selected_p = p[:, indices]
            expected_payout = (selected_p * payout).sum(axis=2)
            mean = expected_payout - total
            variance = np.maximum(0, (selected_p * payout ** 2).sum(axis=2) - expected_payout ** 2)
            profit = (selected_p * (payout > total)).sum(axis=2)
            hit = selected_p.sum(axis=2)
            for i, name in enumerate(names):
                positive = mean[i] > minimum
                if size == 1:
                    keep(f'{name}:max_ev_single', mean[i], positive, indices)
                    keep(f'{name}:max_hit_single', hit[i], positive, indices)
                keep(f'{name}:max_profit_probability', profit[i], positive, indices)
                keep(f'{name}:mean_variance', mean[i] - aversion * variance[i] / (2 * bankroll),
                     positive, indices)
            keep('all_models:consensus', profit.min(axis=0), mean.min(axis=0) > minimum, indices)
    result = {}
    for label, chosen in best.items():
        selected = chosen[1] if chosen else ()
        amounts = np.zeros(len(selections))
        amounts[list(selected)] = unit
        payoffs = amounts * prices - amounts.sum()
        expected = p @ payoffs
        model = label.split(':')[0]
        i = names.index(model) if model in names else None
        model_p = p[i] if i is not None else None
        result[label] = {
            'status': 'CANDIDATE' if selected else 'NO_EDGE', 'stake_yen': int(amounts.sum()),
            'tickets': [{'selection': selections[j], 'stake_yen': unit,
                         'row': {'selection': selections[j], 'odds': float(prices[j])}}
                        for j in selected],
            'research_score': chosen[0] if chosen else 0.0,
            'expected_profit_yen': float(expected[i]) if i is not None else None,
            'worst_model_expected_profit_yen': float(expected.min()),
            'profit_probability': float(model_p @ (payoffs > 0)) if i is not None else None,
            'worst_model_profit_probability': float((p @ (payoffs > 0)).min()),
            'profit_std_yen': float(np.sqrt(model_p @ ((payoffs - expected[i]) ** 2))) if i is not None else None,
            'evaluated_subsets': count,
        }
    return result
