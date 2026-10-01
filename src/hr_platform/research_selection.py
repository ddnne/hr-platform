"""Read-only successor hypotheses over an existing fit, without refitting.

The pointwise minimum is a sensitivity envelope, not a probability distribution
or statistical confidence bound. These choices never create Paper decisions.
"""
import math

from .paper_rules import select


def successors(analysis, base_config, config, previous_quotes=None):
    """Caller verifies the previous observation's as-of, freshness and identity."""
    rows = analysis["rows"]
    variants = [v["probabilities"] for family in ("sensitivity", "weight_sensitivity")
                for v in analysis[family]]
    if any(len(p) != len(rows) for p in variants):
        raise ValueError("SENSITIVITY_SUPPORT")
    if previous_quotes is not None and set(previous_quotes) != {row["selection"] for row in rows}:
        raise ValueError("TREND_SUPPORT")
    fraction = base_config["stake_yen"] / config["reference_bankroll_yen"]
    if not 0 < fraction < 1:
        raise ValueError("RESEARCH_STAKE_FRACTION")
    tolerance = base_config["tie_tolerance"]
    result = {}
    for name in config["variants"]:
        if name == "reference_baseline":
            result[name] = select(rows, "reference", tolerance)
            continue
        if name not in {"reference_sensitivity_floor", "consensus_log_growth", "consensus_log_growth_trend"}:
            raise ValueError("RESEARCH_VARIANT")
        if name.endswith("_trend") and previous_quotes is None:
            result[name] = None
            continue
        choices = []
        for i, row in enumerate(rows):
            lower = min(row["p_ref"], row["p_unregularized"], *(p[i] for p in variants))
            if name.startswith("consensus_"):
                lower = min(lower, row["p_direct"])
            quote, factor = row["odds"], 1.0
            if name.endswith("_trend"):
                previous = previous_quotes[row["selection"]]
                if not math.isfinite(previous) or previous < 1:
                    raise ValueError("TREND_PREVIOUS_QUOTE")
                factor = min(1.0, quote / previous)
                quote *= factor
            adjusted = {**row, "p_ref": lower, "odds": quote}
            if select([adjusted], "reference", tolerance) is None:
                continue
            edge = lower * quote - 1
            score = edge if name == "reference_sensitivity_floor" else (
                lower * math.log1p(fraction * (quote - 1))
                + (1 - lower) * math.log1p(-fraction))
            if score > 0:
                choices.append({**row, "research_probability_floor": lower,
                                "research_projected_odds": quote,
                                "research_trend_factor": factor,
                                "research_score": score})
        if choices:
            best = max(row["research_score"] for row in choices)
            result[name] = min((row for row in choices if best - row["research_score"] <= tolerance),
                               key=lambda row: row["selection"])
        else:
            result[name] = None
    return result
