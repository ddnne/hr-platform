"""Equal-return market marginals and a separate dependence contribution."""
import numpy as np
from scipy import sparse
from scipy.optimize import linprog

from .model import ModelError, key, marginals
from .research_ticket_events import ticket_catalog


def neutral_scenario(event, prices, tolerance):
    """Keep inconsistent marginals visible; never clip or invent a joint Q."""
    hits = np.asarray(event.sum(axis=0)).ravel()
    if not len(hits) or not np.all(hits == hits[0]):
        raise ModelError('NON_CONSTANT_WINNING_COUNT')
    count = float(hits[0])
    gross = count / np.sum(1 / prices)
    marginal = gross / prices
    feasible = bool(np.all(marginal <= 1 + tolerance))
    status = 'FEASIBLE' if feasible else 'NEUTRAL_MARGINALS_NOT_JOINTLY_FEASIBLE'
    # Exclusive tickets partition the state space. Multiple winners require
    # checking the whole event geometry, beyond a sum and individual bounds.
    if feasible and count > 1:
        matrix = sparse.vstack([event, sparse.csr_matrix(np.ones((1, event.shape[1])))])
        result = linprog(np.zeros(event.shape[1]), A_eq=matrix,
                         b_eq=np.r_[marginal, 1.0], bounds=(0, None), method='highs')
        residual = float(np.max(np.abs(matrix @ result.x - np.r_[marginal, 1]))) if result.success else None
        feasible = bool(result.success and residual <= tolerance)
        status = ('FEASIBLE' if feasible else 'NEUTRAL_MARGINALS_NOT_JOINTLY_FEASIBLE'
                  if result.status == 2 else 'NEUTRAL_FEASIBILITY_UNVERIFIED')
    return {'winning_ticket_count_per_state': int(count), 'equal_expected_gross_return': float(gross),
            'probabilities': marginal.tolist(),
            'jointly_feasible': None if status == 'NEUTRAL_FEASIBILITY_UNVERIFIED' else feasible,
            'status': status}


def market_neutral_diagnostics(omega, market, quotes, q_ref, q_marg, *, tolerance,
                               frames=None, place_places=3):
    """Price distortion = dependence + same-marginal price distortion (logs)."""
    selections, event, lower, upper = ticket_catalog(omega, market, quotes, frames=frames,
                                                   place_places=place_places)
    for q in (q_ref, q_marg):
        if len(q) != len(omega) or not np.isfinite(q).all() or np.min(q) < 0 or abs(sum(q) - 1) > tolerance:
            raise ModelError('DIAGNOSTIC_DISTRIBUTION')
    rank_error = float(np.max(np.abs(marginals(omega) @ (np.asarray(q_ref) - np.asarray(q_marg)))))
    if rank_error > tolerance:
        raise ModelError('DIAGNOSTIC_RANK_MARGINALS')
    low = neutral_scenario(event, lower, tolerance)
    high = neutral_scenario(event, upper, tolerance) if np.any(lower != upper) else low
    pref, pmarg = np.asarray(event @ q_ref), np.asarray(event @ q_marg)
    rows = []
    for i, selection in enumerate(selections):
        valid = pref[i] > 0 and pmarg[i] > 0
        v = low['probabilities'][i]
        rows.append({'selection': key(selection), 'p_reference': float(pref[i]), 'p_same_marginals': float(pmarg[i]),
                     'v_neutral_lower_price_scenario': v, 'v_neutral_upper_price_scenario': high['probabilities'][i],
                     'log_price_distortion': float(np.log(pref[i] / v)) if valid else None,
                     'log_dependence_contribution': float(np.log(pref[i] / pmarg[i])) if valid else None,
                     'log_same_marginal_price_distortion': float(np.log(pmarg[i] / v)) if valid else None,
                     'expected_gross_return_lower_price': float(pref[i] * lower[i])})
    return {'market': market, 'rank_marginal_error': rank_error,
            'price_basis': 'DISPLAYED_PRICE_SCENARIO_NOT_FINAL_PRICE_GUARANTEE',
            'lower_price_scenario': low, 'upper_price_scenario': high, 'rows': rows}
