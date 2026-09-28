"""Synthetic numerical timing; never fetches market data."""

import json
import resource
import time
import numpy as np
from hr_platform.model import fit, states, matrix, key, same_marginals

for n in (12, 16):
    omega = states(list(range(1, n + 1)))
    q = np.arange(1, len(omega) + 1, dtype=float)
    q /= q.sum()
    markets = {}
    for h in ("win", "exacta"):
        selections, a = matrix(omega, h)
        markets[h] = {
            "quotes": {
                key(s): {"odds": float(0.8 / p), "display_status": "FIXED"} for s, p in zip(selections, a @ q)
            }
        }
    started = time.perf_counter()
    qr, diagnostics = fit(omega, markets, "quinella", ["win", "exacta"])
    _, dm = same_marginals(omega, qr)
    print(
        json.dumps(
            {
                "kind": "SYNTHETIC",
                "runners": n,
                "states": len(omega),
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "solver_status": diagnostics["status"],
                "marginal_error": dm["marginal_error"],
                "process_peak_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
        )
    )
