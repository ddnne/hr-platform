from collections import Counter
import copy
from itertools import combinations_with_replacement
import json
import math
from pathlib import Path

import pytest

from hr_platform.research_portfolio_scenarios import scenario_portfolios


def inputs():
    p, alternate = [0.75, 0.2, 0.05], [0.2, 0.75, 0.05]
    rows = [{'selection': selection, 'odds': odds, 'p_ref': p[i], 'p_unregularized': p[i],
             'p_marg': alternate[i], 'p_direct': alternate[i]}
            for i, (selection, odds) in enumerate(zip(['1-2', '1-3', '2-3'], [3, 3, 1.2]))]
    analysis = {'rows': rows, 'sensitivity': [{'probabilities': p}],
                'weight_sensitivity': [{'probabilities': alternate}]}
    return (analysis, {'stake_yen': 100, 'probability_tolerance': 1e-7, 'tie_tolerance': 1e-9},
            {'reference_bankroll_yen': 10000},
            json.loads(Path('configs/research-portfolio-scenarios.json').read_text()))


def test_joint_hedge_survives_even_when_each_ticket_fails_worst_case_individually():
    analysis, base, successors, config = inputs()
    before = copy.deepcopy(analysis)
    result = scenario_portfolios(analysis, base, successors, config)['robust_joint']
    assert [(t['selection'], t['stake_yen']) for t in result['tickets']] == [('1-2', 100), ('1-3', 100)]
    assert result['stake_yen'] == 200  # Keeping the remaining 100 is optimal here.
    assert 0.2 * math.log(1.02) + 0.8 * math.log(0.99) < 0
    assert result['research_score'] == pytest.approx(0.95 * math.log(1.01) + 0.05 * math.log(0.98))
    assert result['worst_scenario_expected_profit_yen'] == pytest.approx(85)
    assert result['worst_scenario_hit_probability'] == pytest.approx(0.95)
    assert result['profit_std_yen'] == pytest.approx(math.sqrt(4275))
    assert analysis == before


def test_full_search_matches_explicit_outcome_payoffs_across_batch_boundaries():
    analysis, base, successors, config = inputs()
    config['enumeration_batch_size'] = 2
    results = scenario_portfolios(analysis, base, successors, config)
    distributions = [[0.75, 0.2, 0.05], [0.2, 0.75, 0.05]]
    maxima = {'reference_joint': 0, 'robust_joint': 0}
    for units in range(1, 4):
        for choices in combinations_with_replacement(range(3), units):
            stakes = Counter(choices)
            wealth = [10000 - units * 100 + stakes[i] * 100 * row['odds']
                      for i, row in enumerate(analysis['rows'])]
            scores = [sum(p * math.log(w / 10000) for p, w in zip(distribution, wealth))
                      for distribution in distributions]
            maxima['reference_joint'] = max(maxima['reference_joint'], scores[0])
            maxima['robust_joint'] = max(maxima['robust_joint'], min(scores))
    for variant, result in results.items():
        assert result['research_score'] == pytest.approx(maxima[variant])
        assert result['candidate_count'] == 3
        assert result['evaluated_allocations'] == 19
    larger = scenario_portfolios(analysis, base, successors, {**config, 'enumeration_batch_size': 100})
    assert results == larger


def test_incomplete_or_unnormalized_scenarios_and_duplicate_tickets_are_rejected():
    analysis, base, successors, config = inputs()
    analysis['weight_sensitivity'][0]['probabilities'][0] = 0.1
    with pytest.raises(ValueError, match='PORTFOLIO_SCENARIOS'):
        scenario_portfolios(analysis, base, successors, config)
    analysis, base, successors, config = inputs()
    analysis['rows'][1]['selection'] = analysis['rows'][0]['selection']
    with pytest.raises(ValueError, match='PORTFOLIO_SCENARIOS'):
        scenario_portfolios(analysis, base, successors, config)


def test_near_ties_keep_earlier_candidates_when_a_later_batch_raises_the_maximum():
    analysis, base, successors, config = inputs()
    config['race_budget_yen'] = 100
    config['enumeration_batch_size'] = 2
    p, fraction = 1 / 3, 0.01
    for i, row in enumerate(analysis['rows']):
        score = 0.01 + i * 0.75e-9
        row['odds'] = (math.exp((score - (1 - p) * math.log1p(-fraction)) / p) - 1 + fraction) / fraction
        for field in config['probability_fields']:
            row[field] = p
    for family in config['sensitivity_families']:
        analysis[family] = [{'probabilities': [p, p, p]}]
    small = scenario_portfolios(analysis, base, successors, config)
    large = scenario_portfolios(analysis, base, successors, {**config, 'enumeration_batch_size': 100})
    assert small == large
    assert all(r['tickets'][0]['selection'] == '1-3' for r in small.values())


def test_cash_is_an_option_and_budget_must_fit_the_bankroll():
    analysis, base, successors, config = inputs()
    for row in analysis['rows']:
        row['odds'] = 1
    assert all(not r['tickets'] for r in scenario_portfolios(analysis, base, successors, config).values())
    config['race_budget_yen'] = 10000
    with pytest.raises(ValueError, match='PORTFOLIO_SCENARIO_CONFIG'):
        scenario_portfolios(analysis, base, successors, config)
