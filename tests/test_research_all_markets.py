import copy
import json
from pathlib import Path

import numpy as np
import pytest

from hr_platform.model import key, states
from hr_platform.research_all_markets import all_market_portfolios, offered_markets
from hr_platform.research_ticket_events import winning_selections


def load(name):
    return json.loads(Path('configs/research-' + name + '.json').read_text())


def test_all_nine_markets_keep_reference_exclusion_and_direct_joint_payoff(config):
    options = load('all-markets-kelly')
    options['candidates_per_market'] = 1
    omega = states(list(range(1, 10)))
    frames = {h: min(h, 8) for h in range(1, 10)}
    kelly = load('kelly')
    q = np.full(len(omega), 1 / len(omega))
    estimates = {'omega': omega, 'estimators': {n: {'q': q.tolist()} for n in kelly['pool_weights']}}
    markets = {}
    for market in options['markets']:
        support = {key(t) for s in omega for t in winning_selections(s, market, frames=frames)}
        ranged = market in {'place', 'wide'}
        markets[market] = {'quotes': {s: {'odds': 1000.0 if market == 'trifecta' else 30.0,
            'odds_max': 100.0 if ranged else None, 'display_status': 'RANGE' if ranged else 'FIXED'} for s in support}}
    before = copy.deepcopy(markets)
    result = all_market_portfolios(estimates, markets, '船橋', frames, config, load('successors'),
                                  load('portfolio-scenarios'), kelly, options)
    assert len(result['candidates']) == 24
    assert set(result['all_offered_market_counts']) == set(options['markets'])
    for choice in result['candidates'].values():
        assert not {t['market'] for t in choice['tickets']} & set(choice['calibration_markets'])
        net = np.array([sum(t['stake_yen'] * t['row']['odds'] for t in choice['tickets']
                            if tuple(map(int, t['selection'].split('-'))) in winning_selections(s, t['market'], frames=frames))
                        - choice['stake_yen'] for s in omega])
        assert choice['expected_profit_yen'] == pytest.approx(q @ net)
        assert choice['optimization_log_growth'] == pytest.approx(q @ np.log1p(net / choice['optimization_bankroll_yen']))
        assert choice['stake_yen'] <= 300
    assert markets == before


def test_offered_frame_markets_and_place_small_field_rules():
    options = load('all-markets-kelly')
    assert not any(m.startswith('bracket') for m in offered_markets(list(range(1, 9)), '船橋', options))
    assert 'bracket_quinella' in offered_markets(list(range(1, 10)), '園田', options)
    assert 'bracket_exacta' not in offered_markets(list(range(1, 10)), '園田', options)
    bad = copy.deepcopy(options)
    bad['reference_families']['trifecta'] = ['place']
    with pytest.raises(ValueError, match='ALL_MARKET_CONFIG'):
        offered_markets(list(range(1, 10)), '船橋', bad)
