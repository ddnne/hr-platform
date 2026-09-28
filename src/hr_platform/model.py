"""Market-implied Q, never a claim to calibrated real-world probability P."""

from itertools import permutations
import time
import cvxpy as cp
import numpy as np
from scipy import sparse
from scipy.optimize import linprog
from scipy.special import logsumexp

VERSION = "top3-kl-v1"
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
        value = quote.get("odds")
        if quote.get("display_status") != "FIXED" or value is None or not np.isfinite(value) or value < 1:
            raise ModelError("UNUSABLE_ODDS")
        odds.append(value)
    inverse = 1 / np.array(odds)
    return selections, a, inverse / inverse.sum(), 1 / inverse.sum(), np.array(odds)


def solve(problem, q, tolerance=1e-7, max_iter=300):
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


def fit(omega, markets, target, refs, regularization=1e-4, weights=None, max_iter=300):
    if target in refs or not refs or len(set(refs)) != len(refs) or regularization <= 0:
        raise ModelError("REFERENCE_CONFIG")
    if any(h not in markets for h in refs):
        raise ModelError("REFERENCE_MISSING")
    weights = np.ones(len(refs)) / len(refs) if weights is None else np.asarray(weights, dtype=float)
    if (
        len(weights) != len(refs)
        or not np.isfinite(weights).all()
        or min(weights) < 0
        or abs(sum(weights) - 1) > 1e-10
    ):
        raise ModelError("REFERENCE_WEIGHTS")
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
    data = []
    for h, weight in zip(refs, weights):
        _, a, v, c, _ = reference(omega, h, markets[h]["quotes"])
        objective += weight * cp.sum(cp.kl_div(v, a @ q))
        data.append((h, a, v, c))
    result, diagnostics = solve(cp.Problem(cp.Minimize(objective), [cp.sum(q) == 1]), q, max_iter=max_iter)
    diagnostics["residual_max"] = {h: float(np.max(abs(a @ result - v))) for h, a, v, _ in data}
    diagnostics["common_return"] = {h: float(c) for h, _, _, c in data}
    diagnostics["lambda"] = regularization
    diagnostics["references"] = refs
    diagnostics["weights"] = weights.tolist()
    return result, diagnostics


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


def dependence(omega, q, qmarg):
    runners = sorted(set(x for s in omega for x in s))
    z = np.array([[float(h in s[:a]) for s in omega] for h in runners for a in (1, 2, 3)])
    mu = z @ q
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
    qr, dr = fit(omega, markets, target, refs, config["lambda"], max_iter=config["solver_max_iter"])
    qm, dm = same_marginals(omega, qr, config["solver_max_iter"])
    selections, a, v, _, odds = reference(omega, target, markets[target]["quotes"])
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
    best = max(range(len(rows)), key=lambda i: rows[i]["implied_edge_at_quote"])
    identification = bounds(omega, markets, refs, target, selections[best], config["identification_epsilon"])
    sensitivity = []
    for value in config["sensitivity_lambdas"]:
        qs, ds = fit(omega, markets, target, refs, value, max_iter=config["solver_max_iter"])
        sensitivity.append({"lambda": value, "probabilities": (a @ qs).tolist(), "diagnostics": ds})
    weight_variants = []
    if len(refs) == 2:
        for weights in ([0.25, 0.75], [0.75, 0.25]):
            qw, dw = fit(omega, markets, target, refs, config["lambda"], weights, config["solver_max_iter"])
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
        "identification_selection": key(selections[best]),
        "sensitivity": sensitivity,
        "weight_sensitivity": weight_variants,
        "flags": flags,
        "dependence": dependence(omega, qr, qm),
        "probability_basis": "market_implied_Q",
    }
