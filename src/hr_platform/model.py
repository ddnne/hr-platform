"""Market-implied Q, never a claim to calibrated real-world probability P."""

from itertools import permutations
import time
import numpy as np
from scipy import sparse
from scipy.optimize import linprog
from scipy.special import logsumexp

VERSION = "top3-kl-v3"
MODELED = {"win", "exacta", "quinella", "trifecta", "trio"}


class ModelError(ValueError):
    pass


def states(runners):
    if not 3 <= len(runners) <= 16 or len(set(runners)) != len(runners):
        raise ModelError("RUNNER_COUNT")
    return list(permutations(sorted(runners), 3))


def ticket(state, market):
    if market not in MODELED:
        raise ModelError("UNMODELED_MARKET")
    subset = state[:1] if market == "win" else state[:2] if market in {"exacta", "quinella"} else state
    return tuple(sorted(subset)) if market in {"quinella", "trio"} else tuple(subset)


def key(selection):
    return "-".join(map(str, selection))


def matrix(omega, market):
    selections = sorted({ticket(s, market) for s in omega})
    lookup = {s: i for i, s in enumerate(selections)}
    a = sparse.csr_matrix(
        (np.ones(len(omega)), ([lookup[ticket(s, market)] for s in omega], np.arange(len(omega)))),
        shape=(len(selections), len(omega)),
    )
    return selections, a


def marginals(omega):
    runners = sorted(set(x for s in omega for x in s))
    rows = [
        np.array([s[rank] == horse for s in omega], dtype=float) for horse in runners for rank in range(3)
    ]
    return sparse.csr_matrix(np.array(rows))


def reference(omega, market, quotes):
    selections, a = matrix(omega, market)
    if set(quotes) != {key(s) for s in selections}:
        raise ModelError("INCOMPLETE_MARKET")
    odds = []
    for s in selections:
        quote = quotes[key(s)]
        if not isinstance(quote, dict):
            raise ModelError("UNUSABLE_ODDS")
        value = quote.get("odds")
        if (quote.get("display_status") != "FIXED" or type(value) not in {int, float}
                or not np.isfinite(value) or value < 1):
            raise ModelError("UNUSABLE_ODDS")
        odds.append(value)
    inverse = 1 / np.array(odds)
    return selections, a, inverse / inverse.sum(), 1 / inverse.sum(), np.array(odds)


def solve(problem, q, tolerance=1e-7, max_iter=300):
    import cvxpy as cp

    begin = time.perf_counter()
    try:
        problem.solve(
            solver="CLARABEL", max_iter=max_iter, tol_gap_abs=1e-10, tol_feas=1e-10, tol_gap_rel=1e-10
        )
    except cp.error.SolverError as exc:
        raise ModelError("SOLVER_FAILED") from exc
    if problem.status != cp.OPTIMAL or q.value is None:
        raise ModelError("SOLVER_" + str(problem.status))
    original = np.asarray(q.value).reshape(-1)
    if not np.isfinite(original).all() or original.min() < -tolerance or abs(original.sum() - 1) > tolerance:
        raise ModelError("PROBABILITY_INVALID")
    result = np.maximum(original, 0)
    result /= result.sum()
    diagnostics = {
        "status": problem.status,
        "objective": float(problem.value),
        "duration_ms": (time.perf_counter() - begin) * 1000,
        "min_component_before": float(original.min()),
        "sum_error_before": float(abs(original.sum() - 1)),
        "correction_l1": float(abs(result - original).sum()),
        "iterations": problem.solver_stats.num_iters,
        "solver": "CLARABEL",
        "cvxpy_version": cp.__version__,
    }
    return result, diagnostics


def fit(omega, markets, target, refs, regularization=1e-4, weights=None, max_iter=300, solver="CLARABEL"):
    if (target in refs or not refs or len(set(refs)) != len(refs)
            or not np.isfinite(regularization) or regularization <= 0):
        raise ModelError("REFERENCE_CONFIG")
    if any(h not in markets for h in refs):
        raise ModelError("REFERENCE_MISSING")
    weights = np.ones(len(refs)) / len(refs) if weights is None else np.asarray(weights, dtype=float)
    if (
        weights.ndim != 1
        or len(weights) != len(refs)
        or not np.isfinite(weights).all()
        or min(weights) < 0
        or abs(sum(weights) - 1) > 1e-10
    ):
        raise ModelError("REFERENCE_WEIGHTS")
    if solver not in {"CLARABEL", "WIN_EXACTA_NEWTON_V1"}:
        raise ModelError("SOLVER_CONFIG")
    data = [(h, *reference(omega, h, markets[h]["quotes"])[1:4]) for h in refs]
    if solver == "WIN_EXACTA_NEWTON_V1":
        if target != "quinella" or set(refs) != {"win", "exacta"} or np.any(weights <= 0) or np.any(weights >= 1):
            raise ModelError("NEWTON_REFERENCE_CONFIG")
        if type(max_iter) is not int or max_iter < 1:
            raise ModelError("ITERATION_LIMIT")
        with np.errstate(over="raise", divide="raise", invalid="raise"):
            try:
                result, diagnostics = _win_exacta_newton(omega, data, refs, weights, regularization, max_iter)
            except FloatingPointError as exc:
                raise ModelError("NEWTON_NUMERIC") from exc
    else:
        import cvxpy as cp

        # States with identical reference outcomes must share mass equally at the KL optimum.
        # Collapse these classes exactly, improving conditioning without changing the objective.
        groups = {}
        membership = []
        for s in omega:
            signature = tuple(ticket(s, h) for h in refs)
            if signature not in groups:
                groups[signature] = len(groups)
            membership.append(groups[signature])
        sizes = np.bincount(membership)
        expansion = sparse.csr_matrix(
            (1 / sizes[membership], (np.arange(len(omega)), membership)), shape=(len(omega), len(groups))
        )
        mass = cp.Variable(len(groups), nonneg=True)
        q = expansion @ mass
        u = np.ones(len(omega)) / len(omega)
        objective = regularization * cp.sum(cp.kl_div(q, u))
        for (_, a, v, _), weight in zip(data, weights):
            objective += weight * cp.sum(cp.kl_div(v, a @ q))
        result, diagnostics = solve(cp.Problem(cp.Minimize(objective), [cp.sum(q) == 1]), q, max_iter=max_iter)
    diagnostics["residual_max"] = {h: float(np.max(abs(a @ result - v))) for h, a, v, _ in data}
    diagnostics["common_return"] = {h: float(c) for h, _, _, c in data}
    diagnostics["lambda"] = regularization
    diagnostics["references"] = refs
    diagnostics["weights"] = weights.tolist()
    return result, diagnostics



def _win_exacta_newton(omega, data, refs, weights, regularization, max_iter):
    """Same KL objective on pair masses; full distinct-horse support is required.

    The Hessian is diagonal plus one rank-one block per winner. Its inverse
    supplies the equality-constrained Newton direction without a dense solve.
    The convex tangent bound is an objective diagnostic, not a probability bound.
    """
    begin = time.perf_counter()
    horses = sorted({h for s in omega for h in s})
    if len(omega) != len(horses) * (len(horses) - 1) * (len(horses) - 2) or set(omega) != set(states(horses)):
        raise ModelError("NEWTON_SUPPORT")
    probabilities = {h: v for h, _, v, _ in data}
    a, w = probabilities["exacta"], probabilities["win"]
    alpha, beta = weights[refs.index("win")], weights[refs.index("exacta")]
    first = np.repeat(np.arange(len(horses)), len(horses) - 1)
    afirst = np.bincount(first, weights=a, minlength=len(w))
    x = a * ((alpha * w + beta * afirst) / afirst)[first]
    x /= x.sum()

    def parts(value):
        totals = np.bincount(first, weights=value, minlength=len(w))
        f = regularization * np.sum(value * np.log(value * len(value)))
        f += beta * np.sum(a * np.log(a / value)) + alpha * np.sum(w * np.log(w / totals))
        gradient = regularization * (np.log(value * len(value)) + 1) - beta * a / value - alpha * (w / totals)[first]
        inverse_diagonal = 1 / (regularization / value + beta * a / value**2)
        row_curvature = alpha * w / totals**2
        return f, gradient, inverse_diagonal, row_curvature

    for iteration in range(max_iter):
        objective, gradient, dinv, curvature = parts(x)
        residual = float(np.ptp(gradient))
        gap = float(gradient @ x - gradient.min())
        if not np.isfinite(gradient).all() or not np.isfinite(objective) or not np.isfinite(x).all() or x.min() <= 0:
            raise ModelError("NEWTON_NUMERIC")
        if residual <= 1e-10 and abs(gap) <= 1e-10 and abs(x.sum() - 1) <= 1e-12:
            pairs = sorted({s[:2] for s in omega})
            mapping = {pair: value / (len(horses) - 2) for pair, value in zip(pairs, x)}
            q = np.array([mapping[s[:2]] for s in omega])
            return q, {
                "status": "optimal", "solver": "WIN_EXACTA_NEWTON_V1", "iterations": iteration,
                "objective": float(objective), "stationarity_range": residual,
                "convex_gap_upper": max(0.0, gap), "gap_basis": "FLOATING_POINT_CONVEX_TANGENT_NOT_PROBABILITY_BOUND",
                "duration_ms": (time.perf_counter() - begin) * 1000,
                "min_component_before": float(q.min()), "sum_error_before": float(abs(q.sum() - 1)),
                "correction_l1": 0.0,
            }
        inverse = []
        denominator = 1 + curvature * np.bincount(first, weights=dinv, minlength=len(w))
        for vector in (gradient, np.ones(len(x))):
            weighted = dinv * vector
            adjustment = curvature * np.bincount(first, weights=weighted, minlength=len(w)) / denominator
            inverse.append(weighted - dinv * adjustment[first])
        hg, ho = inverse
        direction = -hg + ho * hg.sum() / ho.sum()
        negative = direction < 0
        step = min(1.0, float(np.min(-0.99 * x[negative] / direction[negative]))) if negative.any() else 1.0
        for _ in range(60):
            candidate = x + step * direction
            candidate /= candidate.sum()
            nf, ng, _, _ = parts(candidate)
            if np.isfinite(nf) and (nf <= objective + 1e-4 * step * (gradient @ direction) or np.ptp(ng) < residual):
                x = candidate
                break
            step *= 0.5
        else:
            raise ModelError("NEWTON_LINE_SEARCH")
    raise ModelError("NEWTON_NOT_CONVERGED")


def win_exacta_unregularized(omega, markets, win_weight=0.5):
    """Analytic lambda=0 forward-KL fit, with maximum-entropy third place.

    Let a_ij be normalized exacta probabilities, a_i their first-place marginal,
    and w_i normalized win probabilities. KL chain rule gives
    r_i = win_weight*w_i + (1-win_weight)*a_i, p_ij = r_i*a_ij/a_i.
    This is a diagnostic limit, not a replacement for the regularized model.
    """
    if not np.isfinite(win_weight) or not 0 < win_weight < 1:
        raise ModelError("REFERENCE_WEIGHTS")
    winners, _, w, _, _ = reference(omega, "win", markets["win"]["quotes"])
    pairs, _, a, _, _ = reference(omega, "exacta", markets["exacta"]["quotes"])
    first = {h[0]: 0.0 for h in winners}
    for (i, _), value in zip(pairs, a):
        first[i] += float(value)
    r = {h[0]: win_weight * value + (1 - win_weight) * first[h[0]]
         for h, value in zip(winners, w)}
    joint = {(i, j): float(value) * r[i] / first[i] for (i, j), value in zip(pairs, a)}
    return np.array([joint[(i, j)] / (len(winners) - 2) for i, j, _ in omega])


def same_marginals(omega, qref, max_iter=300):
    """Maximum entropy on distinct-horse support by iterative proportional fitting.

    Each rank is a categorical partition. Cyclic KL projections from uniform keep
    q in the exponential family and converge to the required I-projection.
    Both marginal feasibility and the entropy dual gap must pass; no tolerance relaxation.
    """
    begin = time.perf_counter()
    qref = np.asarray(qref, dtype=float)
    if not np.isfinite(qref).all() or qref.min() < 0 or abs(qref.sum() - 1) > 1e-10:
        raise ModelError("PROBABILITY_INVALID")
    m = marginals(omega)
    horses = sorted(set(x for s in omega for x in s))
    indices = np.array([[horses.index(h) for h in s] for s in omega])
    target = np.asarray(m @ qref).reshape(len(horses), 3)
    support = np.array(
        [all(target[indices[i, rank], rank] > 0 for rank in range(3)) for i in range(len(omega))]
    )
    if not support.any():
        raise ModelError("MARGINAL_INFEASIBLE")
    q = support.astype(float) / support.sum()
    potentials = np.zeros_like(target)
    for iteration in range(1, max_iter + 1):
        for rank in range(3):
            current = np.bincount(indices[:, rank], weights=q, minlength=len(horses))
            positive = target[:, rank] > 0
            if np.any(current[positive] <= 0):
                raise ModelError("MARGINAL_INFEASIBLE")
            factor = np.ones(len(horses))
            factor[positive] = target[positive, rank] / current[positive]
            q *= factor[indices[:, rank]]
            potentials[positive, rank] += np.log(factor[positive])
        error = float(np.max(abs(m @ q - target.ravel())))
        objective = float(np.sum(q[support] * np.log(q[support] * len(omega))))
        logits = np.array(
            [sum(potentials[indices[i, rank], rank] for rank in range(3)) for i in range(len(omega))]
        ) - np.log(len(omega))
        dual = float(np.sum(target * potentials) - logsumexp(logits[support]))
        gap = abs(objective - dual)
        if error <= 1e-12 and gap <= 1e-9:
            return q, {
                "status": "converged",
                "solver": "categorical_IPF_v1",
                "iterations": iteration,
                "marginal_error": error,
                "objective": objective,
                "dual_gap": gap,
                "sum_error": float(abs(q.sum() - 1)),
                "min_component": float(q.min()),
                "duration_ms": (time.perf_counter() - begin) * 1000,
                "correction_l1": 0.0,
            }
    raise ModelError("MARGINAL_NOT_CONVERGED")


def bounds(omega, markets, refs, target, selection, epsilon=1e-6):
    if target in refs or epsilon < 0:
        raise ModelError("IDENTIFICATION_CONFIG")
    _, a_target = matrix(omega, target)
    selections, _ = matrix(omega, target)
    objective = a_target[selections.index(tuple(selection))].toarray().ravel()
    matrices, limits = [], []
    for h in refs:
        _, a, v, _, _ = reference(omega, h, markets[h]["quotes"])
        matrices.extend([a, -a])
        limits.extend([v + epsilon, -v + epsilon])
    kwargs = dict(
        A_ub=sparse.vstack(matrices),
        b_ub=np.concatenate(limits),
        A_eq=np.ones((1, len(omega))),
        b_eq=[1],
        bounds=(0, None),
        method="highs",
    )
    low, high = linprog(objective, **kwargs), linprog(-objective, **kwargs)
    if low.status == 2 or high.status == 2:
        return {"status": "INCONSISTENT", "lower": None, "upper": None, "epsilon": epsilon}
    if not low.success or not high.success:
        raise ModelError("LP_FAILED")
    width = -high.fun - low.fun
    return {
        "status": "structurally_determined"
        if width < 1e-8
        else "approximately_constrained"
        if width < 1e-3
        else "prior_driven",
        "lower": float(low.fun),
        "upper": float(-high.fun),
        "epsilon": epsilon,
        "interpretation": "constraint_bounds_not_confidence_interval",
    }


def reference_consistency(omega, markets, refs, target, solver_tolerance=None):
    """Minimum common absolute probability slack; independent of KL regularization."""
    if target in refs or not refs or len(set(refs)) != len(refs):
        raise ModelError("REFERENCE_CONFIG")
    options = None
    if solver_tolerance is not None:
        # Optional for old experiment versions. The new solver request must be
        # stricter than the unchanged witness check below, and within HiGHS' range.
        if (type(solver_tolerance) not in {int, float} or not np.isfinite(solver_tolerance)
                or not 1e-10 <= solver_tolerance < 1e-8):
            raise ModelError("CONSISTENCY_SOLVER_CONFIG")
        options = {"primal_feasibility_tolerance": solver_tolerance,
                   "dual_feasibility_tolerance": solver_tolerance}
    references = {h: reference(omega, h, markets[h]["quotes"]) for h in refs}
    a = sparse.vstack([references[h][1] for h in refs], format="csr")
    v = np.concatenate([references[h][2] for h in refs])
    slack = sparse.csr_matrix(-np.ones((len(v), 1)))
    begin = time.perf_counter()
    result = linprog(
        np.r_[np.zeros(len(omega)), 1.0],
        A_ub=sparse.vstack([sparse.hstack([a, slack]), sparse.hstack([-a, slack])]),
        b_ub=np.r_[v, -v], A_eq=np.array([np.r_[np.ones(len(omega)), 0.0]]),
        b_eq=[1.0], bounds=(0, None), method="highs", options=options,
    )
    if not result.success or result.x is None or not np.isfinite(result.x).all():
        raise ModelError("CONSISTENCY_LP_FAILED")
    q, epsilon = result.x[:-1], float(result.x[-1])
    if (q.min() < -1e-8 or epsilon < -1e-8 or abs(q.sum() - 1) > 1e-8
            or np.max(abs(a @ q - v)) > epsilon + 1e-8):
        raise ModelError("CONSISTENCY_WITNESS_INVALID")
    report = {
        "minimum_uniform_absolute_slack": max(0.0, epsilon),
        "units": "probability_not_odds",
        "per_market_max_residual": {
            h: float(np.max(abs(references[h][1] @ q - references[h][2]))) for h in refs
        },
        "solver": "scipy_highs", "duration_ms": (time.perf_counter() - begin) * 1000,
        "interpretation": "reference_compatibility_not_price_edge_or_cause",
        "strategy_threshold_changed": False,
    }
    if solver_tolerance is not None:
        report["solver_feasibility_tolerance"] = solver_tolerance
    if "win" in refs and "exacta" in refs:
        wins, _, vw, _, _ = references["win"]
        pairs, _, ve, _, _ = references["exacta"]
        gaps = [float(vw[i] - sum(ve[j] for j, pair in enumerate(pairs) if pair[0] == horse[0]))
                for i, horse in enumerate(wins)]
        report["win_exacta_first_place"] = {
            "max_absolute_gap": max(map(abs, gaps)), "l1_gap": sum(map(abs, gaps)),
            "necessary_slack_lower_bound": max(map(abs, gaps)) / len(wins),
        }
    return report


def dependence(omega, q, qmarg):
    runners = sorted(set(x for s in omega for x in s))
    z = np.array([[float(h in s[:a]) for s in omega] for h in runners for a in (1, 2, 3)])
    # Explicit reduction avoids spurious dense BLAS floating-point warnings on macOS.
    mu = np.einsum("ik,k->i", z, q, optimize=False)
    joint = np.einsum("ik,k,jk->ij", z, q, z, optimize=False)
    cov = joint - np.outer(mu, mu)
    sd = np.sqrt(np.maximum(mu * (1 - mu), 0))
    denom = np.outer(sd, sd)
    corr = np.divide(cov, denom, out=np.full_like(cov, np.nan), where=denom > 1e-12)
    conditional = np.divide(joint, mu[:, None], out=np.full_like(joint, np.nan), where=mu[:, None] > 1e-12)

    def clean(a):
        return [[None if not np.isfinite(x) else float(x) for x in row] for row in a]

    _, triples = matrix(omega, "trio")
    return {
        "labels": [[h, a] for h in runners for a in (1, 2, 3)],
        "covariance": cov.tolist(),
        "correlation": clean(corr),
        "conditional": clean(conditional),
        "triple_probabilities": (triples @ q).tolist(),
        "triple_difference": (triples @ (q - qmarg)).tolist(),
        "basis": "under_Q_not_empirical",
    }


def analyze(runners, markets, config):
    omega = states(runners)
    target, refs = config["target"], config["references"]
    if target == "quinella" and "exacta" not in refs or target == "trio" and "trifecta" not in refs:
        raise ModelError("JOINT_REFERENCE_MISSING")
    # Validate the target before spending time on calibration; all entry points
    # share the same quote checks, including JSON booleans masquerading as 1.
    selections, a, v, _, odds = reference(omega, target, markets[target]["quotes"])
    qr, dr = fit(omega, markets, target, refs, config["lambda"], max_iter=config["solver_max_iter"], solver=config["solver"])
    qm, dm = same_marginals(omega, qr, config["solver_max_iter"])
    direct_market = "exacta" if target == "quinella" else "trifecta"
    direct_selections, _, direct_v, _, _ = reference(omega, direct_market, markets[direct_market]["quotes"])
    direct = {s: 0.0 for s in selections}
    for selection, value in zip(direct_selections, direct_v):
        direct[tuple(sorted(selection))] += float(value)
    pr, pm = a @ qr, a @ qm
    rows = []
    for i, selection in enumerate(selections):
        valid = bool(min(pr[i], pm[i], v[i]) > 1e-12)
        rows.append(
            {
                "selection": key(selection),
                "odds": float(odds[i]),
                "p_ref": float(pr[i]),
                "p_marg": float(pm[i]),
                "p_direct": direct[selection],
                "v_target": float(v[i]),
                "d_price": float(np.log(pr[i] / v[i])) if valid else None,
                "d_dep": float(np.log(pr[i] / pm[i])) if valid else None,
                "d_rest": float(np.log(pm[i] / v[i])) if valid else None,
                "implied_edge_at_quote": float(pr[i] * odds[i] - 1),
                "stress_scenario_only": [
                    float(pr[i] * odds[i] * (1 - d) - 1) for d in config["stress_fractions"]
                ],
                "valid_log": valid,
            }
        )
    calibration = None
    if target == "quinella" and set(refs) == {"win", "exacta"}:
        q0 = win_exacta_unregularized(omega, markets)
        p0 = a @ q0
        calibration = {
            "basis": "ANALYTIC_UNREGULARIZED_FORWARD_KL_LIMIT",
            "win_weight": 0.5,
            "third_place_basis": "MAX_ENTROPY_UNIDENTIFIED_BY_WIN_EXACTA",
            "probabilities": p0.tolist(),
            "interpretation": "ALGEBRAIC_PROBABILITY_DIFFERENCE_NOT_CAUSAL_OR_PROFIT",
        }
        for row, baseline in zip(rows, p0):
            row["p_unregularized"] = float(baseline)
            row["reference_market_adjustment"] = float(baseline - row["p_direct"])
            row["regularization_adjustment"] = float(row["p_ref"] - baseline)
    best = max(range(len(rows)), key=lambda i: rows[i]["implied_edge_at_quote"])
    identification = bounds(omega, markets, refs, target, selections[best], config["identification_epsilon"])
    consistency = reference_consistency(omega, markets, refs, target, config.get("consistency_solver_tolerance"))
    sensitivity = []
    for value in config["sensitivity_lambdas"]:
        qs, ds = fit(omega, markets, target, refs, value, max_iter=config["solver_max_iter"], solver=config["solver"])
        sensitivity.append({"lambda": value, "probabilities": (a @ qs).tolist(), "diagnostics": ds})
    weight_variants = []
    if len(refs) == 2:
        for weights in ([0.25, 0.75], [0.75, 0.25]):
            qw, dw = fit(omega, markets, target, refs, config["lambda"], weights, config["solver_max_iter"], config["solver"])
            weight_variants.append(
                {"weights": weights, "probabilities": (a @ qw).tolist(), "diagnostics": dw}
            )
    sign = np.sign(pr * odds - 1)
    flags = []
    if any(np.any(np.sign(np.array(s["probabilities"]) * odds - 1) != sign) for s in sensitivity):
        flags.append("REGULARIZATION_SENSITIVE")
    if any(np.max(abs(np.array(s["probabilities"]) - pr)) > 0.01 for s in weight_variants):
        flags.append("REFERENCE_SENSITIVE")
    return {
        "model_version": VERSION,
        "omega": omega,
        "q_ref": qr.tolist(),
        "q_marg": qm.tolist(),
        "rows": rows,
        "reference_diagnostics": dr,
        "marginal_diagnostics": dm,
        "identification": identification,
        "reference_consistency": consistency,
        "calibration_decomposition": calibration,
        "identification_selection": key(selections[best]),
        "sensitivity": sensitivity,
        "weight_sensitivity": weight_variants,
        "flags": flags,
        "dependence": dependence(omega, qr, qm),
        "probability_basis": "market_implied_Q",
    }
