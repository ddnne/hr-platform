"""Nine-market research portfolios; purchased markets never calibrate that Q."""
from .model import ModelError, reference, same_marginals
from .research_joint_kelly import joint_kelly_portfolios
from .research_ticket_events import MARKETS, ticket_catalog


def offered_markets(runners, venue, config):
    """Ordinary pre-withdrawal entry roster and documented single-race rules."""
    if (set(config['markets']) != MARKETS or len(config['markets']) != len(MARKETS)
            or config['range_price_policy'] != 'DISPLAYED_LOWER_BOUND_FOR_PURCHASE_ACTUAL_PUBLISHED_PAYOUT_FOR_EVALUATION'
            or config['reference_families'] != {'win_exacta': ['win', 'exacta'], 'trifecta': ['trifecta']}
            or config['reference_selection'] != 'largest_optimization_log_growth_tie_prefers_win_exacta'
            or type(config['place_two_paid_max_runners']) is not int or config['place_two_paid_max_runners'] < 3
            or type(config['bracket_min_runners_without_withdrawals']) is not int
            or config['bracket_min_runners_without_withdrawals'] < 3):
        raise ModelError('ALL_MARKET_CONFIG')
    frames_sold = len(runners) >= config['bracket_min_runners_without_withdrawals']
    return [m for m in config['markets'] if not m.startswith('bracket')
            or frames_sold and (m != 'bracket_exacta' or venue in config['bracket_exacta_venues'])]


def all_market_portfolios(estimates, markets, venue, frames, base, successors, portfolio, kelly, config):
    omega = [tuple(s) for s in estimates['omega']]
    runners = sorted({h for s in omega for h in s})
    offered = offered_markets(runners, venue, config)
    places = 2 if len(runners) <= config['place_two_paid_max_runners'] else 3
    if set(markets) != set(offered):
        raise ModelError('ALL_MARKETS_REQUIRED')
    for market in offered:
        ticket_catalog(omega, market, markets[market]['quotes'], frames=frames, place_places=places)
    selections, _, probabilities, _, _ = reference(omega, 'trifecta', markets['trifecta']['quotes'])
    if selections != omega:
        raise ModelError('TRIFECTA_STATE_ORDER')
    qmarg, diagnostics = same_marginals(omega, probabilities, base['solver_max_iter'])
    families = {'win_exacta': estimates, 'trifecta': {'omega': omega, 'estimators': {
        'reference': {'q': probabilities.tolist()}, 'marginal': {'q': qmarg.tolist()}}}}
    proposals = {}
    for family, refs in config['reference_families'].items():
        options = {**config, 'markets': [m for m in offered if m not in refs]}
        allocation = {**portfolio, 'enumeration_batch_size': config['enumeration_batch_size']}
        weights = kelly if family == 'win_exacta' else {**kelly, 'pool_weights': config['trifecta_pool_weights']}
        rows = joint_kelly_portfolios(families[family], markets, refs, base, successors, allocation, weights,
                                     options, frames=frames, place_places=places)
        for label, choice in rows.items():
            label = label.removeprefix('joint_kelly_')
            proposals[f'all_{family}_{label}'] = {**choice, 'reference_family': family,
                'calibration_markets': refs, 'offered_markets': offered, 'place_paid_positions': places,
                'price_basis': config['range_price_policy']}
    for method in config['methods']:
        for fraction in config['capital_fractions']:
            label = method + '_' + fraction
            choices = [proposals[f'all_{family}_{label}'] for family in families]
            best = choices[0]
            for choice in choices[1:]:
                if choice['optimization_log_growth'] > best['optimization_log_growth'] + base['tie_tolerance']:
                    best = choice
            proposals['all_select_' + label] = {**best, 'reference_selection_rule': config['reference_selection']}
    return {'candidates': proposals, 'trifecta_marginal_diagnostics': diagnostics,
            'all_offered_market_counts': {m: len(markets[m]['quotes']) for m in offered}}
