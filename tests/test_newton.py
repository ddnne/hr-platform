import copy
import cvxpy as cp
import numpy as np
import pytest
from fixtures.synthetic.solver_cases import capacity_markets
from hr_platform import fixtures
from hr_platform.model import analyze, fit, reference, states, ModelError

SOLVER = "WIN_EXACTA_NEWTON_V1"


@pytest.mark.parametrize("regularization", [1e-6, 1e-4, 1e-2])
@pytest.mark.parametrize("alpha", [0.25, 0.5, 0.75])
def test_known_regularized_optimum(regularization, alpha):
    omega = states([1, 2, 3, 4])
    pairs = sorted({state[:2] for state in omega})
    known = np.arange(1, len(pairs)+1, dtype=float)
    known /= known.sum()
    entropy = np.sum(known * np.log(known * len(pairs)))
    # Choose observations so the objective's gradient is constant at known.
    exacta = known * (1 + regularization / (1-alpha) * (np.log(known * len(pairs)) - entropy))
    wins = np.bincount([pair[0]-1 for pair in pairs], weights=known)
    markets = {
        "win": {"quotes": {str(i+1): {"odds": float(1/p), "display_status": "FIXED"} for i, p in enumerate(wins)}},
        "exacta": {"quotes": {f"{i}-{j}": {"odds": float(1/p), "display_status": "FIXED"}
                              for (i, j), p in zip(pairs, exacta)}},
    }
    q, _ = fit(omega, markets, "quinella", ["win", "exacta"], regularization,
               [alpha, 1-alpha], solver=SOLVER)
    expected = np.array([known[pairs.index(state[:2])] / 2 for state in omega])
    np.testing.assert_allclose(q, expected, atol=1e-10, rtol=0)


@pytest.mark.parametrize("regularization", [1e-6, 1e-4, 1e-2])
@pytest.mark.parametrize("alpha", [0.25, 0.5, 0.75])
def test_identical_full_state_objective_and_stationarity(regularization, alpha):
    omega = states([1, 2, 3, 4])
    markets = fixtures.markets("nonuniform")
    markets["win"]["quotes"]["1"]["odds"] = 1.2  # inconsistent references remain inconsistent
    q, d = fit(omega, markets, "quinella", ["win", "exacta"], regularization,
               [alpha, 1-alpha], solver=SOLVER)
    variable = cp.Variable(len(omega), nonneg=True)
    objective = regularization * cp.sum(cp.kl_div(variable, np.ones(len(omega)) / len(omega)))
    gradient = regularization * (np.log(q * len(q)) + 1)
    for market, weight in [("win", alpha), ("exacta", 1-alpha)]:
        _, a, v, _, _ = reference(omega, market, markets[market]["quotes"])
        objective += weight * cp.sum(cp.kl_div(v, a @ variable))
        gradient += weight * np.asarray(a.T @ (1 - v / (a @ q))).ravel()
    # Evaluate the full-state objective independently; a cone solver
    # with weak entropy regularization is not an exact probability oracle.
    variable.value = q
    assert d["objective"] == pytest.approx(objective.value, abs=1e-12)
    assert np.ptp(gradient) <= 1e-10
    assert d["stationarity_range"] <= 1e-10 and d["convex_gap_upper"] <= 1e-10
    assert q.min() > 0 and abs(q.sum() - 1) <= 1e-12


@pytest.mark.parametrize("n", [4, 9, 12, 16])
def test_whole_analysis_capacity_cases(n, config):
    config["solver"] = SOLVER
    result = analyze(list(range(1, n+1)), capacity_markets(list(range(1, n+1))), config)
    diagnostics = [result["reference_diagnostics"]] + [
        s["diagnostics"] for s in result["sensitivity"] + result["weight_sensitivity"]]
    assert len(diagnostics) == 5 and all(d["solver"] == SOLVER for d in diagnostics)
    assert all(d["status"] == "optimal" and d["convex_gap_upper"] <= 1e-10 for d in diagnostics)
    assert result["marginal_diagnostics"]["marginal_error"] <= 1e-12


def test_uniform_analytic_solution_target_exclusion_and_reference_order():
    omega = states([1, 2, 3, 4])
    markets = fixtures.markets("uniform")
    q, _ = fit(omega, markets, "quinella", ["win", "exacta"], solver=SOLVER)
    np.testing.assert_allclose(q, np.ones(24)/24, atol=1e-14)
    markets = fixtures.markets("nonuniform")
    q, _ = fit(omega, markets, "quinella", ["win", "exacta"], weights=[0.25, 0.75], solver=SOLVER)
    changed = copy.deepcopy(markets)
    changed["quinella"]["quotes"]["1-2"]["odds"] *= 10
    other, _ = fit(omega, changed, "quinella", ["exacta", "win"], weights=[0.75, 0.25], solver=SOLVER)
    np.testing.assert_array_equal(q, other)


@pytest.mark.parametrize("regularization", [-1, 0, float("nan"), float("inf")])
def test_nonconvex_or_nonfinite_regularization_rejected(regularization):
    with pytest.raises(ModelError, match="REFERENCE_CONFIG"):
        fit(states([1, 2, 3, 4]), fixtures.markets("uniform"), "quinella", ["win", "exacta"],
            regularization, solver=SOLVER)


@pytest.mark.parametrize("weights", [[0.5, 0.7], [-0.1, 1.1], [0, 1], [1, 0], [0.5], [[0.5], [0.5]]])
def test_invalid_or_single_informative_reference_weights_rejected(weights):
    with pytest.raises(ModelError, match="REFERENCE"):
        fit(states([1, 2, 3, 4]), fixtures.markets(), "quinella", ["win", "exacta"],
            weights=weights, solver=SOLVER)


def test_unsupported_references_support_and_iteration_exhaustion_rejected():
    omega = states([1, 2, 3, 4])
    markets = fixtures.markets("nonuniform")
    for refs in (["win", "win"], ["exacta"]):
        with pytest.raises(ModelError, match="REFERENCE"):
            fit(omega, markets, "quinella", refs, solver=SOLVER)
    with pytest.raises(ModelError, match="NEWTON_SUPPORT"):
        fit(omega[:-1], markets, "quinella", ["win", "exacta"], solver=SOLVER)
    with pytest.raises(ModelError, match="NEWTON_NOT_CONVERGED"):
        fit(omega, markets, "quinella", ["win", "exacta"], max_iter=1, solver=SOLVER)


def test_default_solver_is_unchanged(config):
    result = analyze([1, 2, 3, 4], fixtures.markets(), config)
    diagnostics = [result["reference_diagnostics"]] + [
        s["diagnostics"] for s in result["sensitivity"] + result["weight_sensitivity"]]
    assert all(d["solver"] == "CLARABEL" for d in diagnostics)
