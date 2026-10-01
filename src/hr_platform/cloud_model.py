"""Bounded model-only RPC payload. No clock eligibility, decisions or persistence.

Scientific packages load inside the request: SciPy imports consume entropy,
which the Python Workers runtime disallows during its startup snapshot.
"""

import json
import math
import platform
import time
import warnings

MAX_INPUT_BYTES = 1024 * 1024
VERSION = "python-worker-model-v4"


def clock_timings(value):
    """A frozen runtime clock cannot establish zero execution time."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "duration_ms" and v is not None and v <= 0:
                value[k] = None
            else:
                clock_timings(v)
    elif isinstance(value, list):
        for v in value:
            clock_timings(v)
    return value


def validate(payload):
    if not isinstance(payload, str) or len(payload.encode()) > MAX_INPUT_BYTES:
        raise ValueError("INPUT_LIMIT")
    data = json.loads(payload)
    if not isinstance(data, dict) or set(data) != {"runners", "markets", "config"}:
        raise ValueError("INPUT_SCHEMA")
    runners, markets, config = data["runners"], data["markets"], data["config"]
    if (
        not isinstance(runners, list)
        or not 3 <= len(runners) <= 16
        or any(type(h) is not int or not 1 <= h <= 16 for h in runners)
        or len(set(runners)) != len(runners)
        or not isinstance(markets, dict)
        or not isinstance(config, dict)
    ):
        raise ValueError("INPUT_SCHEMA")
    if type(config.get("solver_max_iter")) is not int or not 1 <= config["solver_max_iter"] <= 300:
        raise ValueError("ITERATION_LIMIT")
    if (
        config.get("solver") not in {"CLARABEL", "WIN_EXACTA_NEWTON_V1"}
        or config.get("target") not in {"quinella", "trio"}
        or not isinstance(config.get("references"), list)
        or not 1 <= len(config["references"]) <= 4
        or any(h not in {"win", "exacta", "quinella", "trio", "trifecta"} for h in config["references"])
    ):
        raise ValueError("MODEL_CONFIG")
    for name, limit in (("sensitivity_lambdas", 3), ("stress_fractions", 3)):
        values = config.get(name)
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError("MODEL_CONFIG")
        if any(type(x) not in {int, float} or not math.isfinite(x) for x in values):
            raise ValueError("MODEL_CONFIG")
    if any(x <= 0 for x in config["sensitivity_lambdas"]) or any(
        not 0 <= x <= 1 for x in config["stress_fractions"]
    ):
        raise ValueError("MODEL_CONFIG")
    for name in ("lambda", "identification_epsilon"):
        value = config.get(name)
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ValueError("MODEL_CONFIG")
    return data


def execute(payload):
    started = time.perf_counter()
    envelope = {
        "runtime_adapter": VERSION,
        "purpose": "MODEL_ONLY_NOT_PAPER_DECISION",
        "paper_decision_created": False,
        "live_execution_qualified": False,
        "timing_basis": "RUNTIME_CLOCK_ELAPSED_NOT_BILLED_CPU",
    }
    try:
        data = validate(payload)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return json.dumps({**envelope, "status": "INPUT_ERROR"})
    try:
        from .model import analyze, ModelError
        import cvxpy
        import clarabel
        import scipy
        import numpy

        try:
            with warnings.catch_warnings():
                # Failure remains MODEL_ERROR. Avoid CVXPY's duplicate warning
                # in Worker logs; no payloads or exception text are returned.
                warnings.filterwarnings(
                    "ignore", message=r"Solution may be inaccurate\.", category=UserWarning
                )
                result = analyze(**data)
        except ModelError as exc:
            from .paper_rules import model_error_reason
            return json.dumps({**envelope, "status": model_error_reason(exc)})
        status = (
            "REFERENCE_INCONSISTENT" if result["identification"]["status"] == "INCONSISTENT" else "ANALYZED"
        )
        return json.dumps(
            clock_timings(
                {
                    **envelope,
                    "status": status,
                    "analysis": result,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                    "runtime_versions": {
                        "python": platform.python_version(),
                        "cvxpy": cvxpy.__version__,
                        "clarabel": clarabel.__version__,
                        "scipy": scipy.__version__,
                        "numpy": numpy.__version__,
                    },
                }
            ),
            allow_nan=False,
        )
    except Exception:
        # RPC failures must not put payloads or scientific tracebacks in logs.
        return json.dumps({**envelope, "status": "RUNTIME_ERROR"})
