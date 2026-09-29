import math
import numpy as np
import pytest
from hr_platform import fixtures as f
from hr_platform.model import states, reference_consistency, dependence, ModelError


def test_compatible_references_need_no_slack_and_target_is_excluded():
    omega = states([1, 2, 3, 4])
    markets = f.markets()
    result = reference_consistency(omega, markets, ['win', 'exacta'], 'quinella')
    assert result['minimum_uniform_absolute_slack'] == pytest.approx(0, abs=1e-8)
    markets['quinella']['quotes'] = {}
    again = reference_consistency(omega, markets, ['win', 'exacta'], 'quinella')
    assert again['minimum_uniform_absolute_slack'] == result['minimum_uniform_absolute_slack']
    with pytest.raises(ModelError, match='REFERENCE_CONFIG'):
        reference_consistency(omega, markets, ['win', 'quinella'], 'quinella')


def test_known_marginal_discrepancy_has_analytic_minimum():
    markets = f.markets('uniform')
    for horse, probability in enumerate([.29, .21, .25, .25], 1):
        markets['win']['quotes'][str(horse)]['odds'] = 1 / probability
    result = reference_consistency(states([1, 2, 3, 4]), markets, ['win', 'exacta'], 'quinella')
    # Four error terms: one win ticket plus three outgoing exacta tickets.
    assert result['win_exacta_first_place']['max_absolute_gap'] == pytest.approx(.04)
    assert result['minimum_uniform_absolute_slack'] == pytest.approx(.01)
    assert result['win_exacta_first_place']['necessary_slack_lower_bound'] == pytest.approx(.01)
    assert not result['strategy_threshold_changed']


def test_large_field_dependency_reduction_matches_scalar_sums():
    omega = states(list(range(1, 13)))
    q = np.arange(1, len(omega) + 1, dtype=float)
    q /= q.sum()
    with np.errstate(all='raise'):
        result = dependence(omega, q, q)
    labels = result['labels']
    covariance = np.array(result['covariance'])
    mu = [math.fsum(float(q[i]) for i, s in enumerate(omega) if h in s[:rank])
          for h, rank in labels]
    np.testing.assert_allclose(np.diag(covariance), np.array(mu)*(1-np.array(mu)), atol=1e-12)
    assert np.isfinite(covariance).all()
    np.testing.assert_allclose(result['triple_difference'], 0, atol=1e-12)
