"""Small mutually exclusive ticket portfolios over an unchanged saved Qref fit."""
from itertools import combinations, product
import math

from .research_selection import successors


def growth(tickets, bankroll):
    """One quinella outcome pays; all stakes are lost on an unselected outcome."""
    fraction = sum(t['stake_yen'] for t in tickets) / bankroll
    if not 0 < fraction < 1:
        raise ValueError('PORTFOLIO_STAKE_FRACTION')
    cash = 1 - fraction
    return math.log(cash) + math.fsum(
        t['row']['p_ref'] * math.log1p(t['stake_yen'] / bankroll * t['row']['odds'] / cash)
        for t in tickets)


def ranked_candidates(analysis, base, successors_config, config):
    """Reuse the unit-stake gates; the sensitivity floor remains an envelope."""
    candidates = []
    for index, row in enumerate(analysis['rows']):
        one = {'rows': [row], **{family: [{**variant, 'probabilities': [variant['probabilities'][index]]}
               for variant in analysis[family]] for family in ('sensitivity', 'weight_sensitivity')}}
        selected = successors(one, base, {**successors_config, 'variants': [config['candidate_variant']]})[config['candidate_variant']]
        if selected is not None:
            candidates.append(selected)
    candidates.sort(key=lambda r: (-r['research_score'], r['selection']))
    return candidates[:config['candidate_cap']]


def ranked_tickets(analysis, base, successors_config, config):
    candidates = ranked_candidates(analysis, base, successors_config, config)
    tickets = [{'selection': r['selection'], 'stake_yen': config['stake_unit_yen'], 'row': r}
               for r in candidates[:config['max_tickets_per_race']]]
    return {'tickets': tickets, 'stake_yen': sum(t['stake_yen'] for t in tickets)}


def portfolio(analysis, base, successors_config, config, *, max_tickets=None):
    """Use the same candidate pool/budget for one ticket and several tickets."""
    unit, budget, cap = (config[k] for k in ('stake_unit_yen', 'race_budget_yen', 'candidate_cap'))
    limit = config['max_tickets_per_race'] if max_tickets is None else max_tickets
    if (any(type(n) is not int for n in (unit, budget, cap, limit))
            or unit != base['stake_yen'] or budget <= 0 or budget % unit
            or cap < 1 or not 1 <= limit <= config['max_tickets_per_race']
            or config['candidate_variant'] != 'consensus_log_growth'
            or config['target'] != 'quinella'):
        raise ValueError('PORTFOLIO_CONFIG')
    rows = analysis['rows']
    probabilities = [r['p_ref'] for r in rows]
    if (len({r['selection'] for r in rows}) != len(rows)
            or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities)
            or abs(math.fsum(probabilities) - 1) > base['probability_tolerance']):
        raise ValueError('PORTFOLIO_DISTRIBUTION')
    bankroll = successors_config['reference_bankroll_yen']
    if not 0 < budget < bankroll:
        raise ValueError('PORTFOLIO_STAKE_FRACTION')
    candidates = ranked_candidates(analysis, base, successors_config, config)
    best, best_score = [], 0.0
    units = budget // unit
    tolerance = base['tie_tolerance']
    for size in range(1, min(limit, len(candidates), units) + 1):
        for subset in combinations(candidates, size):
            for allocation in product(range(1, units + 1), repeat=size):
                if sum(allocation) != units:
                    continue
                tickets = sorted(({'selection': r['selection'], 'stake_yen': n * unit, 'row': r}
                                  for r, n in zip(subset, allocation)), key=lambda t: t['selection'])
                score = growth(tickets, bankroll)
                key = [(t['selection'], t['stake_yen']) for t in tickets]
                old_key = [(t['selection'], t['stake_yen']) for t in best]
                if score > 0 and (not best or score > best_score + tolerance or abs(score - best_score) <= tolerance and key < old_key):
                    best, best_score = tickets, score
    return {'tickets': best, 'research_score': best_score, 'candidate_count': len(candidates),
            'stake_yen': sum(t['stake_yen'] for t in best)}
