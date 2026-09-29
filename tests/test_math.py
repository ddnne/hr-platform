import copy
import numpy as np
import pytest
from hr_platform import fixtures as f
from hr_platform.model import (
    states,
    matrix,
    marginals,
    fit,
    same_marginals,
    bounds,
    analyze,
    ModelError,
    dependence,
    reference,
)


def test_partition_and_conversion():
    omega = states([1, 2, 3, 4])
    assert len(omega) == 24 and all(len(set(s)) == 3 for s in omega)
    for h in ("win", "exacta", "quinella", "trio", "trifecta"):
        _, a = matrix(omega, h)
        assert np.all(a.sum(axis=0) == 1)
        assert np.isclose((a @ f.distribution()).sum(), 1)
    q = f.distribution()
    np.testing.assert_allclose(marginals(omega) @ q, 0.25)
    selections, a = matrix(omega, "quinella")
    np.testing.assert_allclose(a @ q, [7 / 30 if s in [(1, 2), (3, 4)] else 2 / 15 for s in selections])
    qm, d = same_marginals(omega, q)
    np.testing.assert_allclose(qm, np.ones(24) / 24, atol=1e-7)
    assert d["marginal_error"] < 1e-7


def test_target_exclusion_and_decomposition(config):
    markets = f.markets()
    omega = states([1, 2, 3, 4])
    q, _ = fit(omega, markets, "quinella", ["win", "exacta"])
    markets["quinella"]["quotes"]["1-2"]["odds"] *= 10
    q2, _ = fit(omega, markets, "quinella", ["win", "exacta"])
    np.testing.assert_array_equal(q, q2)
    with pytest.raises(ModelError):
        fit(omega, markets, "quinella", ["quinella"])
    result = analyze([1, 2, 3, 4], markets, config)
    for row in result["rows"]:
        assert row["d_price"] == pytest.approx(row["d_dep"] + row["d_rest"])
    assert result["rows"][0]["d_dep"] > 0
    assert result["identification"]["status"] == "approximately_constrained"


def test_nonuniform_shrinkage_rounding():
    omega = states([1, 2, 3, 4])
    markets = f.markets("nonuniform")
    qlo, _ = fit(omega, markets, "quinella", ["win", "exacta"], 1e-6)
    qhi, _ = fit(omega, markets, "quinella", ["win", "exacta"], 1e-2)
    _, a = matrix(omega, "quinella")
    truth = a @ f.distribution("nonuniform")
    assert np.max(abs(a @ qlo - truth)) < 1e-5
    assert np.max(abs(a @ qhi - truth)) > 1e-4
    rounded, _ = fit(omega, f.markets("nonuniform", rounded=True), "quinella", ["win", "exacta"], 1e-6)
    assert np.max(abs(a @ rounded - truth)) > 1e-5


def test_label_permutation():
    omega = states([1, 2, 3, 4])
    markets = f.markets("nonuniform")
    qr, _ = fit(omega, markets, "quinella", ["win", "exacta"])
    permutation = {1: 4, 2: 2, 3: 1, 4: 3}
    changed = copy.deepcopy(markets)
    for h, data in markets.items():
        changed[h]["quotes"] = {}
        for k, quote in data["quotes"].items():
            nums = [permutation[int(x)] for x in k.split("-")]
            if h in {"quinella", "trio"}:
                nums.sort()
            changed[h]["quotes"]["-".join(map(str, nums))] = quote
    q2, _ = fit(omega, changed, "quinella", ["win", "exacta"])
    for i, s in enumerate(omega):
        assert qr[i] == pytest.approx(q2[omega.index(tuple(permutation[x] for x in s))], abs=1e-6)


def test_partial_identification_and_inconsistency():
    omega = states([1, 2, 3, 4])
    m = f.markets("uniform")
    assert bounds(omega, m, ["win"], "quinella", (1, 2), 0)["status"] == "prior_driven"
    determined = bounds(omega, m, ["exacta"], "quinella", (1, 2), 0)
    assert determined["upper"] == pytest.approx(1 / 6)
    assert determined["lower"] == pytest.approx(1 / 6)
    m["win"]["quotes"]["1"]["odds"] = 1.1
    assert bounds(omega, m, ["win", "exacta"], "quinella", (1, 2), 0)["status"] == "INCONSISTENT"


def test_incomplete_ranges_zero_rejected():
    omega = states([1, 2, 3, 4])
    for alteration in ("missing", "range", "zero"):
        quotes = f.markets()["exacta"]["quotes"]
        if alteration == "missing":
            del quotes["1-2"]
        elif alteration == "range":
            quotes["1-2"]["display_status"] = "RANGE"
        else:
            quotes["1-2"]["odds"] = 0
        with pytest.raises(ModelError):
            reference(omega, "exacta", quotes)


def test_solver_failure_is_not_success():
    with pytest.raises(ModelError):
        fit(states([1, 2, 3, 4]), f.markets("nonuniform"), "quinella", ["win", "exacta"], max_iter=1)


def test_degenerate_correlation():
    omega = states([1, 2, 3])
    q = np.ones(6) / 6
    result = dependence(omega, q, q)
    assert result["correlation"][2][2] is None  # certain top-three event


def test_trio_separate_experiment(config):
    config.update(target="trio", references=["win", "exacta", "trifecta"])
    result = analyze([1, 2, 3, 4], f.markets("nonuniform"), config)
    assert len(result["rows"]) == 4
    np.testing.assert_allclose(
        [r["p_direct"] for r in result["rows"]], [r["v_target"] for r in result["rows"]]
    )


def test_max_entropy_nonuniform_analytic_exponential_family():
    omega = states([1, 2, 3, 4])
    potentials = np.array([[0.7, -0.2, 0.1], [-0.4, 0.5, 0.8], [0.2, -0.7, -0.3], [0.4, 0.6, -0.5]])
    logits = np.array([sum(potentials[horse - 1, rank] for rank, horse in enumerate(s)) for s in omega])
    known = np.exp(logits - logits.max())
    known /= known.sum()
    actual, d = same_marginals(omega, known)
    np.testing.assert_allclose(actual, known, atol=1e-9, rtol=1e-9)
    assert d["dual_gap"] <= 1e-9
    with pytest.raises(ModelError, match="MARGINAL_NOT_CONVERGED"):
        same_marginals(omega, known, max_iter=1)


def test_max_entropy_boundary_support():
    omega = states([1, 2, 3, 4])
    known = np.array([0.5 if s in [(1, 2, 3), (1, 3, 2)] else 0 for s in omega])
    actual, d = same_marginals(omega, known)
    np.testing.assert_allclose(actual, known, atol=1e-12)
    assert d["marginal_error"] <= 1e-12


@pytest.mark.parametrize('weight', [0.25, 0.5, 0.75])
def test_unregularized_projection_matches_independent_convex_solution(weight):
    import cvxpy as cp
    from hr_platform.model import win_exacta_unregularized

    omega = states([1, 2, 3, 4])
    markets = f.markets('nonuniform')
    markets['win']['quotes']['1']['odds'] = 1.2  # intentionally inconsistent references
    q0 = win_exacta_unregularized(omega, markets, weight)
    pairs = sorted({s[:2] for s in omega})
    pair_mass = cp.Variable(len(pairs), nonneg=True)
    expansion = np.array([[0.5 if state[:2] == pair else 0 for pair in pairs] for state in omega])
    q = expansion @ pair_mass
    objective = 0
    for market, w in [('win', weight), ('exacta', 1 - weight)]:
        _, a, v, _, _ = reference(omega, market, markets[market]['quotes'])
        objective += w * cp.sum(cp.kl_div(v, a @ q))
    problem = cp.Problem(cp.Minimize(objective), [cp.sum(q) == 1])
    problem.solve(solver='CLARABEL', tol_gap_abs=1e-11, tol_feas=1e-11, tol_gap_rel=1e-11)
    assert problem.status == cp.OPTIMAL
    for market in ('win', 'exacta', 'quinella'):
        _, a = matrix(omega, market)
        np.testing.assert_allclose(a @ q0, a @ q.value, atol=2e-6)
    assert q0.min() > 0 and q0.sum() == pytest.approx(1)
    # The third-place distribution is a stated maximum-entropy choice, not extra evidence.
    for i, j, k in omega:
        values = [q0[n] for n, state in enumerate(omega) if state[:2] == (i, j)]
        assert max(values) == min(values)


def test_calibration_decomposition_excludes_target_and_preserves_direct(config):
    from hr_platform.model import win_exacta_unregularized

    markets = f.markets('nonuniform')
    omega = states([1, 2, 3, 4])
    q0 = win_exacta_unregularized(omega, markets)
    _, a = matrix(omega, 'quinella')
    np.testing.assert_allclose(a @ q0, a @ f.distribution('nonuniform'), atol=1e-12)
    markets['quinella']['quotes']['1-2']['odds'] *= 5
    np.testing.assert_array_equal(q0, win_exacta_unregularized(omega, markets))
    markets['win']['quotes']['1']['odds'] = 1.2
    result = analyze([1, 2, 3, 4], markets, config)
    for row in result['rows']:
        assert row['p_ref'] - row['p_direct'] == pytest.approx(
            row['reference_market_adjustment'] + row['regularization_adjustment'], abs=1e-15)
    assert max(abs(r['reference_market_adjustment']) for r in result['rows']) > 1e-3
    assert result['identification']['status'] == 'INCONSISTENT'
