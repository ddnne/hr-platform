import copy
import json
from pathlib import Path

import pytest

from hr_platform.research_selection import successors


def setup():
    config = json.loads(Path("configs/research-successors.json").read_text())
    probabilities = [0.003, 0.15, 0.847]
    rows = [{"selection": selection, "odds": odds, "v_target": target, "valid_log": True,
             "p_ref": p, "p_unregularized": p, "p_direct": p}
            for selection, odds, target, p in zip(
                ["1-2", "1-3", "2-3"], [1000, 10, 1.1], [0.001, 0.1, 0.899], probabilities)]
    analysis = {"rows": rows, "sensitivity": [{"probabilities": probabilities}],
                "weight_sensitivity": [{"probabilities": probabilities}]}
    return analysis, {"stake_yen": 100, "tie_tolerance": 1e-9}, config


def test_growth_penalizes_payoff_concentration_without_changing_fit():
    analysis, base, config = setup()
    original = copy.deepcopy(analysis)
    result = successors(analysis, base, config)
    assert result["reference_baseline"]["selection"] == "1-2"
    assert result["reference_sensitivity_floor"]["selection"] == "1-2"
    assert result["consensus_log_growth"]["selection"] == "1-3"
    assert result["consensus_log_growth_trend"] is None
    assert analysis == original


def test_reference_variants_and_direct_probability_can_remove_apparent_edge():
    analysis, base, config = setup()
    analysis["sensitivity"][0]["probabilities"] = [0.0005, 0.15, 0.8495]
    analysis["rows"][1]["p_direct"] = 0.09
    result = successors(analysis, base, config)
    assert result["reference_sensitivity_floor"]["selection"] == "1-3"
    assert result["consensus_log_growth"] is None


def test_trend_missing_support_is_rejected_and_price_drop_can_remove_bet():
    analysis, base, config = setup()
    with pytest.raises(ValueError, match="TREND_SUPPORT"):
        successors(analysis, base, config, {"1-2": 1000})
    result = successors(analysis, base, config, {"1-2": 1000, "1-3": 20, "2-3": 1.1})
    assert result["consensus_log_growth"]["selection"] == "1-3"
    assert result["consensus_log_growth_trend"] is None
