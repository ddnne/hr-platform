import copy
import json
import math
from pathlib import Path

import pytest

from hr_platform.research_expansions import conditional_portfolio, expansions
from hr_platform.research_portfolio_scenarios import scenario_portfolios


def inputs():
    p, alternate = [0.75, 0.2, 0.05], [0.2, 0.75, 0.05]
    rows = [{'selection': s, 'odds': o, 'v_target': 0.1, 'valid_log': True,
             'p_ref': p[i], 'p_unregularized': p[i], 'p_marg': alternate[i], 'p_direct': alternate[i]}
            for i, (s, o) in enumerate(zip(['1-2', '1-3', '2-3'], [3, 3, 1.2]))]
    return ({'rows': rows, 'sensitivity': [{'probabilities': p}], 'weight_sensitivity': [{'probabilities': alternate}]},
            {'stake_yen': 100, 'probability_tolerance': 1e-7, 'tie_tolerance': 1e-9},
            {'reference_bankroll_yen': 10000},
            json.loads(Path('configs/research-portfolio-scenarios.json').read_text()),
            json.loads(Path('configs/research-expansions.json').read_text()))


def test_positive_conditional_edge_does_not_justify_unconditional_deterioration():
    analysis, base, successors, portfolio, _ = inputs()
    choice = conditional_portfolio(analysis, base, successors, portfolio)
    assert 0.2 / (1 - 0.75) * 3 > 1  # Positive only after assuming the anchor loses.
    assert [t['selection'] for t in choice['tickets']] == ['1-2']
    assert choice['research_score'] == pytest.approx(0.75 * math.log(1.02) + 0.25 * math.log(0.99))


def test_multistage_backup_uses_exclusive_joint_outcomes_and_can_rescue_an_anchor():
    analysis, base, successors, portfolio, _ = inputs()
    choice = conditional_portfolio(analysis, base, successors, portfolio, robust=True)
    assert [t['selection'] for t in choice['tickets']] == ['1-2', '1-3']
    assert choice['stake_yen'] == 200
    assert choice['research_score'] == pytest.approx(0.95 * math.log(1.01) + 0.05 * math.log(0.98))
    assert choice['expected_profit_yen'] == pytest.approx(85)
    assert choice['hit_probability'] == pytest.approx(0.95)
    assert choice['profit_std_yen'] == pytest.approx(math.sqrt(4275))
    assert choice['steps'][1]['conditional_probability'][0] == pytest.approx(0.8)


def test_hedge_and_dependence_variants_preserve_inputs_and_observed_prices():
    analysis, base, successors, portfolio, config = inputs()
    before = copy.deepcopy(analysis)
    result = expansions(analysis, base, successors, portfolio, config,
                        previous_quotes={r['selection']: r['odds'] * 2 for r in analysis['rows']}, elapsed_seconds=120)
    assert len(result) == 6 and analysis == before
    for variant in ('trend_damped_joint', 'trend_adverse_joint'):
        assert result[variant]['trend_projection_exponent'] == pytest.approx(0.25)
        assert result[variant]['tickets']
        for ticket in result[variant]['tickets']:
            assert ticket['research_projected_odds'] == pytest.approx(max(1, ticket['row']['odds'] * 2 ** -0.25))
    assert result['dependence_half']['stake_yen'] == 300
    assert result['dependence_half']['expected_profit_yen'] == pytest.approx(127.5)  # (0.475 * 3 - 1) * 300
    assert result['dependence_marginal']['tickets'][0]['selection'] == '1-3'


def test_reference_endpoint_reuses_exact_search_and_dominates_greedy_on_same_objective():
    analysis, base, successors, portfolio, config = inputs()
    config['dependence_reference_weights']['dependence_half'] = 1.0
    result = expansions(analysis, base, successors, portfolio, config)
    old = scenario_portfolios(analysis, base, successors, portfolio)['reference_joint']
    assert result['dependence_half']['research_score'] == old['research_score']
    assert result['dependence_half']['stake_yen'] == old['stake_yen']
    assert old['research_score'] >= result['conditional_reference']['research_score'] - base['tie_tolerance']
    assert result['trend_damped_joint']['status'] == 'INPUT_EXCLUDED'


def test_unprofitable_prefix_is_proposal_only_and_invalid_inputs_are_rejected():
    analysis, base, successors, portfolio, config = inputs()
    portfolio['race_budget_yen'] = 100
    choice = conditional_portfolio(analysis, base, successors, portfolio, robust=True)
    assert choice['stake_yen'] == 0 and not choice['tickets'] and choice['proposal']['steps']
    with pytest.raises(ValueError, match='TREND_INPUT'):
        expansions(analysis, base, successors, portfolio, config, previous_quotes={'1-2': 3}, elapsed_seconds=120)
    analysis['rows'][0]['p_ref'] = 0.9
    with pytest.raises(ValueError, match='EXPANSION_DISTRIBUTIONS'):
        expansions(analysis, base, successors, portfolio, config)


def test_trend_upside_is_ignored_only_in_adverse_variant():
    analysis, base, successors, portfolio, config = inputs()
    previous = {r['selection']: r['odds'] for r in analysis['rows']}
    previous['1-2'] = 1.5
    result = expansions(analysis, base, successors, portfolio, config, previous_quotes=previous, elapsed_seconds=60)
    assert any(t['research_projected_odds'] > t['row']['odds'] for t in result['trend_damped_joint']['tickets'])
    assert all(t['research_projected_odds'] <= t['row']['odds'] for t in result['trend_adverse_joint']['tickets'])
