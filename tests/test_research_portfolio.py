import copy
import json
import math
from pathlib import Path

import pytest

from hr_platform.research_portfolio import growth, portfolio, ranked_tickets


def inputs():
    p = [0.4, 0.4, 0.2]
    rows = [{'selection': s, 'odds': o, 'p_ref': q, 'p_unregularized': q, 'p_direct': q,
             'v_target': v, 'valid_log': True} for s, o, q, v in zip(
                 ['1-2', '1-3', '2-3'], [3, 3, 1.2], p, [0.3, 0.3, 0.4])]
    analysis = {'rows': rows, 'sensitivity': [{'probabilities': p}],
                'weight_sensitivity': [{'probabilities': p}]}
    return (analysis, {'stake_yen': 100, 'tie_tolerance': 1e-9, 'probability_tolerance': 1e-7},
            json.loads(Path('configs/research-successors.json').read_text()),
            json.loads(Path('configs/research-portfolio.json').read_text()))


def test_two_exclusive_winners_use_total_portfolio_stake():
    a, _, _, _ = inputs()
    tickets = [{'row': a['rows'][0], 'stake_yen': 100}, {'row': a['rows'][1], 'stake_yen': 200}]
    expected = 0.4 * math.log(1.0) + 0.4 * math.log(1.03) + 0.2 * math.log(0.97)
    assert growth(tickets, 10000) == pytest.approx(expected)


def test_multiple_tickets_improve_growth_with_the_same_budget_and_input():
    a, base, successors, config = inputs()
    before = copy.deepcopy(a)
    single = portfolio(a, base, successors, config, max_tickets=1)
    multiple = portfolio(a, base, successors, config)
    assert len(single['tickets']) == 1 and len(multiple['tickets']) == 2
    assert single['stake_yen'] == multiple['stake_yen'] == 300
    assert {t['stake_yen'] for t in multiple['tickets']} == {100, 200}
    assert multiple['research_score'] > single['research_score']
    assert a == before


def test_unusable_probability_distribution_and_duplicate_ticket_are_rejected():
    a, base, successors, config = inputs()
    a['rows'][0]['p_ref'] = 0.9
    with pytest.raises(ValueError, match='PORTFOLIO_DISTRIBUTION'):
        portfolio(a, base, successors, config)
    a, base, successors, config = inputs()
    a['rows'][1]['selection'] = a['rows'][0]['selection']
    with pytest.raises(ValueError, match='PORTFOLIO_DISTRIBUTION'):
        portfolio(a, base, successors, config)


def test_no_qualifying_edge_keeps_cash_and_bankroll_is_not_overdrawn():
    a, base, successors, config = inputs()
    for row in a['rows']:
        row['v_target'] = 1
    assert not portfolio(a, base, successors, config)['tickets']
    config['race_budget_yen'] = successors['reference_bankroll_yen']
    with pytest.raises(ValueError, match='PORTFOLIO_STAKE_FRACTION'):
        portfolio(a, base, successors, config)


def test_ranked_multiple_tickets_keep_the_original_unit_stake():
    a, base, successors, config = inputs()
    result = ranked_tickets(a, base, successors, config)
    assert len(result['tickets']) == 2 and result['stake_yen'] == 200
    assert all(t['stake_yen'] == 100 for t in result['tickets'])
    assert {t['selection'] for t in result['tickets']} == {'1-2', '1-3'}
