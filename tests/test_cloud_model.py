import json
from pathlib import Path
import importlib.util
import pytest
from hr_platform import fixtures as f
from hr_platform.cloud_model import execute, MAX_INPUT_BYTES
from hr_platform.model import ModelError, analyze
from hr_platform.parser import parse_odds


def payload(config):
    race = parse_odds(f.archive(), {f.RACE: f.state()})[f.RACE]
    return {"runners": race["state"]["runners"], "markets": race["markets"], "config": config}


def test_wrapper_uses_existing_model_and_does_not_claim_live_paper(config):
    p = payload(config)
    result = json.loads(execute(json.dumps(p)))
    native = analyze(**p)
    assert result["status"] == "ANALYZED"
    assert result["analysis"]["q_ref"] == native["q_ref"]
    assert result["analysis"]["q_marg"] == native["q_marg"]
    assert result["analysis"]["rows"] == native["rows"]
    assert not result["paper_decision_created"] and not result["live_execution_qualified"]
    assert result["runtime_versions"]["clarabel"] and result["duration_ms"] > 0


@pytest.mark.parametrize(
    "kind",
    [
        "too_large",
        "bad_json",
        "unknown_key",
        "too_many_horses",
        "bad_horse",
        "iterations",
        "nan",
        "sensitivities",
        "stress",
        "solver",
        "refs",
    ],
)
def test_limits_reject_before_scientific_execution(config, monkeypatch, kind):
    from hr_platform import model

    monkeypatch.setattr(model, "analyze", lambda **_: pytest.fail("invalid input reached solver"))
    p = payload(config)
    if kind == "too_many_horses":
        p["runners"] = list(range(1, 18))
    elif kind == "bad_horse":
        p["runners"][0] = True
    elif kind == "unknown_key":
        p["private_extra"] = "sensitive input"
    else:
        updates = {
            "iterations": {"solver_max_iter": 301},
            "nan": {"lambda": float("nan")},
            "sensitivities": {"sensitivity_lambdas": [1e-4] * 4},
            "stress": {"stress_fractions": [-1]},
            "solver": {"solver": "OTHER"},
            "refs": {"references": ["win"] * 5},
        }
        p["config"].update(updates.get(kind, {}))
    raw = (
        "x" * (MAX_INPUT_BYTES + 1)
        if kind == "too_large"
        else "sensitive invalid JSON"
        if kind == "bad_json"
        else json.dumps(p)
    )
    result = execute(raw)
    assert json.loads(result)["status"] == "INPUT_ERROR" and "sensitive" not in result


@pytest.mark.parametrize(
    "error,status",
    [(ModelError("sensitive payload"), "MODEL_ERROR"), (RuntimeError("sensitive payload"), "RUNTIME_ERROR")],
)
def test_failures_return_static_status_without_payload_or_traceback(
    config, monkeypatch, capsys, error, status
):
    from hr_platform import model

    def fail(**_):
        raise error

    monkeypatch.setattr(model, "analyze", fail)
    result = execute(json.dumps(payload(config)))
    assert json.loads(result)["status"] == status
    assert "sensitive" not in result and "Traceback" not in result
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_inconsistent_references_keep_successful_model_distinct(config):
    p = payload(config)
    p["markets"]["win"]["quotes"]["1"]["odds"] = 1.01
    result = json.loads(execute(json.dumps(p)))
    assert result["status"] == "REFERENCE_INCONSISTENT"
    assert result["analysis"]["reference_diagnostics"]["status"] == "optimal"
    assert result["analysis"]["identification"]["lower"] is None


@pytest.mark.parametrize("value", [True, False, "2.0", float("nan"), float("inf"), None])
@pytest.mark.parametrize("market", ["quinella", "exacta", "win"])
def test_invalid_prices_rejected_by_shared_model_before_solver(config, monkeypatch, value, market):
    from hr_platform import model

    monkeypatch.setattr(model, "solve", lambda *_a, **_k: pytest.fail("invalid price reached solver"))
    p = payload(config)
    next(iter(p["markets"][market]["quotes"].values()))["odds"] = value
    with pytest.raises(ModelError, match="UNUSABLE_ODDS"):
        analyze(**p)
    assert json.loads(execute(json.dumps(p)))["status"] == "MODEL_ERROR"


def test_actual_solver_failure_has_static_response_without_warning(config, capsys, recwarn):
    p = payload(config)
    p["config"]["solver_max_iter"] = 1
    result = json.loads(execute(json.dumps(p)))
    assert result["status"] == "MODEL_ERROR" and "analysis" not in result
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    assert not recwarn


def test_staging_copies_only_allowlisted_sources_and_refuses_existing_directory(tmp_path, monkeypatch):
    path = Path("scripts/prepare_python_worker.py").resolve()
    spec = importlib.util.spec_from_file_location("prepare_python_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "stage"
    monkeypatch.setattr(module, "private_root", lambda _: root)
    assert module.prepare("ignored-test") == root
    assert {p.relative_to(root / "src").as_posix() for p in (root / "src").rglob("*") if p.is_file()} == {
        "entry.py",
        "hr_platform/__init__.py",
        "hr_platform/model.py",
        "hr_platform/cloud_model.py",
    }
    assert (root / "src/hr_platform/model.py").read_bytes() == Path("src/hr_platform/model.py").read_bytes()
    config = json.loads((root / "wrangler.jsonc").read_text())
    assert not config["workers_dev"] and not config["preview_urls"] and config["triggers"]["crons"] == []
    assert not {"r2_buckets", "d1_databases", "services"} & config.keys()
    with pytest.raises(ValueError, match="FRESH_BUILD"):
        module.prepare("ignored-test")
