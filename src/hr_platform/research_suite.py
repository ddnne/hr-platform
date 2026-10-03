"""Shared research choices for complete snapshots; no network, clock or ledger I/O."""
from copy import deepcopy

from .model import ModelError, analyze, fit, matrix
from .paper_rules import reference_eligibility, select
from .research_all_markets import all_market_portfolios
from .research_alternatives import estimate, purchases
from .research_expansions import expansions
from .research_joint_kelly import joint_kelly_portfolios
from .research_kelly import kelly_portfolios
from .research_portfolio import portfolio, ranked_tickets
from .research_portfolio_scenarios import scenario_portfolios
from .research_selection import successors


def base_candidates(analysis, base, configs, *, previous_quotes=None, elapsed_seconds=None, baseline_error=None):
    """Caller qualifies full, earlier observations before supplying trend prices."""
    successor_config = configs['successors']
    if baseline_error:
        successor_config = {**successor_config, 'variants': ['reference_baseline']}
    original = successors(analysis, base, successor_config, previous_quotes)
    results = {name: {'status': 'INPUT_EXCLUDED' if name.endswith('_trend') and previous_quotes is None
                     else 'CANDIDATE' if row else 'NO_EDGE',
                     'stake_yen': base['stake_yen'] if row else 0,
                     'tickets': [{'selection': row['selection'], 'stake_yen': base['stake_yen'], 'row': row}]
                     if row else []} for name, row in original.items()}
    if baseline_error:
        for name in [*configs['successors']['variants'], *configs['portfolio']['variants'],
                     *configs['portfolio-scenarios']['variants'], *configs['expansions']['variants']]:
            if name not in results:
                results[name] = {'status': 'INPUT_EXCLUDED', 'reason': baseline_error, 'stake_yen': 0, 'tickets': []}
        return results
    for name in ([] if baseline_error else configs['portfolio']['variants']):
        choice = (ranked_tickets(analysis, base, configs['successors'], configs['portfolio'])
                  if name == 'ranked_up_to_three_100' else
                  portfolio(analysis, base, configs['successors'], configs['portfolio'],
                            max_tickets=1 if name == 'single_300' else configs['portfolio']['max_tickets_per_race']))
        results[name] = {**choice, 'status': 'CANDIDATE' if choice['stake_yen'] else 'NO_EDGE'}
    results.update({name: {**choice, 'status': 'CANDIDATE' if choice['stake_yen'] else 'NO_EDGE'}
                    for name, choice in scenario_portfolios(analysis, base, configs['successors'],
                                                           configs['portfolio-scenarios']).items()})
    results.update(expansions(analysis, base, configs['successors'], configs['portfolio-scenarios'],
                              configs['expansions'], previous_quotes=previous_quotes, elapsed_seconds=elapsed_seconds))
    return results


def candidate_suite(runners, markets, analysis, base, configs, *, previous_quotes=None,
                    elapsed_seconds=None, venue='', frames=None, actual_offered=None,
                    place_places=None, win_probability_source='market', baseline_regularization=None):
    """Reuse estimators and allocations, recording explicit sport adaptations.

    The optional small positive KL baseline is a finite proxy. It is never
    described as the analytic zero-regularization result used by win/exacta.
    """
    if analysis['reference_diagnostics']['references'] != base['references']:
        raise ModelError('SAVED_REFERENCE_MISMATCH')
    reason, assumptions = reference_eligibility(analysis, base)
    if reason:
        raise ModelError(reason)
    work = deepcopy(analysis)
    baseline = None
    baseline_error = None
    if any('p_unregularized' not in row for row in work['rows']):
        if baseline_regularization is None or not 0 < baseline_regularization < base['lambda']:
            raise ModelError('FINITE_BASELINE_CONFIG_REQUIRED')
        try:
            q, diagnostics = fit([tuple(s) for s in work['omega']], markets, base['target'], base['references'],
                                 baseline_regularization, max_iter=base['solver_max_iter'], solver='CLARABEL')
            _, event = matrix([tuple(s) for s in work['omega']], base['target'])
            for row, probability in zip(work['rows'], event @ q):
                row['p_unregularized'] = float(probability)
            baseline = {'basis': 'FINITE_POSITIVE_KL_PROXY_NOT_ANALYTIC_ZERO_LIMIT', 'diagnostics': diagnostics}
        except ModelError as error:
            baseline_error = str(error)
            baseline = {'basis': 'FINITE_POSITIVE_KL_PROXY_NOT_ANALYTIC_ZERO_LIMIT', 'error': baseline_error}
    candidates = base_candidates(work, base, configs, previous_quotes=previous_quotes,
                                 elapsed_seconds=elapsed_seconds, baseline_error=baseline_error)
    estimates = estimate(runners, markets, work, base, configs['alternatives'],
                         win_probability_source=win_probability_source)
    candidates.update(purchases(estimates, markets['quinella']['quotes'], base, configs['successors'],
                                configs['portfolio-scenarios'], configs['alternatives']))
    candidates.update(kelly_portfolios(estimates, markets['quinella']['quotes'], base, configs['successors'],
                                      configs['portfolio-scenarios'], configs['kelly']))
    # Every estimator's inputs must be excluded, including the alternatives
    # whose pair/win inputs can be wider than the primary fit's references.
    refs = list(dict.fromkeys([*base['references'], 'exacta',
                              *(['win'] if win_probability_source == 'market' else [])]))
    excluded = {'finite_baseline': baseline_error} if baseline_error else {}
    try:
        if not set(configs['joint-kelly']['markets']) <= set(markets):
            raise ModelError('INCOMPLETE_MARKET')
        candidates.update(joint_kelly_portfolios(estimates, markets, refs, base, configs['successors'],
                                                 configs['portfolio-scenarios'], configs['kelly'], configs['joint-kelly']))
    except ModelError as error:
        excluded['joint_kelly'] = str(error)
    try:
        if actual_offered is not None and 'place' in actual_offered and place_places is None:
            raise ModelError('PLACE_RULE_UNVERIFIED')
        all_markets = all_market_portfolios(estimates, markets, venue, frames or {}, base, configs['successors'],
                                            configs['portfolio-scenarios'], configs['kelly'], configs['all-markets-kelly'],
                                            actual_offered=actual_offered,
                                            calibration_refs=None if refs == ['win', 'exacta'] else refs,
                                            place_places=place_places)
        candidates.update(all_markets['candidates'])
    except ModelError as error:
        excluded['all_markets'] = str(error)
        all_markets = None
    for method in ('direct', 'marginal', 'reference'):
        choice = select(analysis['rows'], method, base['tie_tolerance'])
        candidates['native_' + method] = {'tickets': [{'selection': choice['selection'], 'stake_yen': base['stake_yen'],
                                                      'row': choice}] if choice else [],
                                          'stake_yen': base['stake_yen'] if choice else 0}
    trio_candidates, trio, trio_error = _trio_candidates(runners, markets, base)
    candidates.update(trio_candidates)
    if trio_error:
        excluded['trio'] = trio_error
    return {'candidates': candidates, 'estimators': estimates, 'finite_baseline': baseline,
            'all_market_diagnostics': all_markets, 'trio_analysis': trio, 'family_exclusions': excluded,
            'win_probability_source': win_probability_source, 'used_reference_markets': refs,
            'research_assumptions': assumptions}


def _trio_candidates(runners, markets, base):
    trio_config = {**base, 'target': 'trio', 'references': ['win', 'trifecta'] if 'win' in markets
                   else ['exacta', 'trifecta'], 'solver': 'CLARABEL'}
    candidates = {}
    try:
        if not {trio_config['target'], *trio_config['references']} <= set(markets):
            raise ModelError('INCOMPLETE_MARKET')
        trio = analyze(runners, markets, trio_config)
        reason, assumptions = reference_eligibility(trio, trio_config)
        if reason:
            raise ModelError(reason)
        for method in ('direct', 'marginal', 'reference'):
            choice = select(trio['rows'], method, base['tie_tolerance'])
            candidates['trio_' + method] = {'tickets': [{'market': 'trio', 'selection': choice['selection'],
                                                       'stake_yen': base['stake_yen'], 'row': choice}] if choice else [],
                                            'stake_yen': base['stake_yen'] if choice else 0,
                                            'research_assumptions': assumptions}
    except ModelError as error:
        return candidates, None, str(error)
    return candidates, trio, None


def strategy_names(configs, primary='win_exacta'):
    """Keep unavailable strategies in the same registry as calculated ones."""
    names = [*configs['successors']['variants'], *configs['portfolio']['variants'],
             *configs['portfolio-scenarios']['variants'], *configs['expansions']['variants']]
    names += [f'{m}:{p}' for m in configs['kelly']['pool_weights'] for p in configs['alternatives']['policies']]
    names += ['all_models:consensus']
    names += [f'kelly_{m}_{f}' for m in configs['kelly']['methods'] for f in configs['kelly']['capital_fractions']]
    names += [f'joint_kelly_{m}_{f}' for m in configs['joint-kelly']['methods']
              for f in configs['joint-kelly']['capital_fractions']]
    names += [f'all_{family}_{m}_{f}' for family in (primary, 'trifecta', 'select')
              for m in configs['all-markets-kelly']['methods'] for f in configs['all-markets-kelly']['capital_fractions']]
    names += [prefix + m for prefix in ('native_', 'trio_') for m in ('direct', 'marginal', 'reference')]
    return names


def analyze_suite(runners, markets, base, configs, **options):
    """Qualify independent target families independently; no zero-price filling."""
    primary = 'win_exacta' if base['references'] == ['win', 'exacta'] else 'primary'
    primary_error = None
    try:
        if not {base['target'], *base['references']} <= set(markets):
            raise ModelError('INCOMPLETE_MARKET')
        analysis = analyze(runners, markets, base)
        result = candidate_suite(runners, markets, analysis, base, configs, **options)
    except ModelError as error:
        primary_error = str(error)
        candidates, trio, trio_error = _trio_candidates(runners, markets, base)
        result = {'candidates': candidates, 'family_exclusions': {'primary': primary_error},
                  'trio_analysis': trio}
        if trio_error:
            result['family_exclusions']['trio'] = trio_error
        analysis = None
    for name in strategy_names(configs, primary):
        if name not in result['candidates']:
            family = ('trio' if name.startswith('trio_') else 'all_markets' if name.startswith('all_')
                      and name != 'all_models:consensus' else 'joint_kelly' if name.startswith('joint_kelly_') else 'primary')
            result['candidates'][name] = {'status': 'INPUT_EXCLUDED', 'reason': result['family_exclusions'].get(family, primary_error),
                                          'tickets': [], 'stake_yen': 0}
    return {**result, 'primary_analysis': analysis, 'primary_error': primary_error}
