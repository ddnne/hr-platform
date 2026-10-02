import numpy as np
import pytest
from types import SimpleNamespace

from hr_platform.model import same_marginals, states
from hr_platform.research_neutral import market_neutral_diagnostics, neutral_scenario
from hr_platform.research_ticket_events import ticket_catalog
from test_research_ticket_events import quotes


def test_multiple_hits_equal_return_and_dependence_decomposition():
    omega = states([1, 2, 3, 4])
    ref = np.arange(1, len(omega) + 1, dtype=float)
    ref /= ref.sum()
    marginal, _ = same_marginals(omega, ref)
    for market, count in [('place', 2), ('wide', 3)]:
        result = market_neutral_diagnostics(omega, market, quotes(omega, market, place_places=2),
                                            ref, marginal, tolerance=1e-8, place_places=2)
        low, high = result['lower_price_scenario'], result['upper_price_scenario']
        assert low['jointly_feasible'] and high['jointly_feasible']
        assert np.isclose(sum(low['probabilities']), count)
        assert np.allclose(np.array(low['probabilities']) * 2, low['equal_expected_gross_return'])
        assert np.isclose(high['equal_expected_gross_return'], low['equal_expected_gross_return'] * 2)
        for row in result['rows']:
            assert np.isclose(row['log_price_distortion'], row['log_dependence_contribution']
                              + row['log_same_marginal_price_distortion'])


def test_inconsistent_wide_neutral_marginals_are_retained():
    omega = states([1, 2, 3, 4])
    q = np.ones(len(omega)) / len(omega)
    prices = quotes(omega, 'wide')
    # Every individual marginal is <=1 and their sum is 3. A three-horse
    # triangle cannot contain all three edges of a four-horse star.
    for selection, quote in prices.items():
        price = 3 if selection in {'1-2', '1-3', '1-4'} else 300
        quote.update(odds=price, odds_max=None, display_status='FIXED')
    result = market_neutral_diagnostics(omega, 'wide', prices, q, q, tolerance=1e-8)
    scenario = result['lower_price_scenario']
    assert max(scenario['probabilities']) < 1
    assert np.isclose(sum(scenario['probabilities']), 3)
    assert not scenario['jointly_feasible']
    assert scenario['status'] == 'NEUTRAL_MARGINALS_NOT_JOINTLY_FEASIBLE'


def test_different_rank_marginals_and_solver_failure_are_not_dependence_evidence(monkeypatch):
    omega = states([1, 2, 3, 4])
    q = np.ones(len(omega)) / len(omega)
    other = q.copy()
    other[0] += 0.01
    other[-1] -= 0.01
    prices = quotes(omega, 'place', place_places=2)
    with pytest.raises(ValueError, match='DIAGNOSTIC_RANK_MARGINALS'):
        market_neutral_diagnostics(omega, 'place', prices, q, other, tolerance=1e-8, place_places=2)
    _, event, lower, _ = ticket_catalog(omega, 'place', prices, place_places=2)
    monkeypatch.setattr('hr_platform.research_neutral.linprog', lambda *a, **kw: SimpleNamespace(success=False, status=4))
    result = neutral_scenario(event, lower, 1e-8)
    assert result['jointly_feasible'] is None and result['status'] == 'NEUTRAL_FEASIBILITY_UNVERIFIED'
