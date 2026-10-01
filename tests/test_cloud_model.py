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


def test_explicit_newton_selection_uses_shared_math_and_fails_closed(config):
    p = payload(config)
    p["config"]["solver"] = "WIN_EXACTA_NEWTON_V1"
    result = json.loads(execute(json.dumps(p)))
    assert result["status"] == "ANALYZED"
    assert result["analysis"]["q_ref"] == analyze(**p)["q_ref"]
    assert result["analysis"]["reference_diagnostics"]["solver"] == "WIN_EXACTA_NEWTON_V1"
    assert not result["paper_decision_created"]
    p["config"]["solver_max_iter"] = 1
    assert json.loads(execute(json.dumps(p)))["status"] == "MODEL_ERROR"


def test_nonadvancing_runtime_clock_does_not_claim_zero_computation_time(config, monkeypatch):
    from hr_platform import cloud_model

    monkeypatch.setattr(cloud_model.time, "perf_counter", lambda: 42.0)
    result = json.loads(execute(json.dumps(payload(config))))
    assert result["status"] == "ANALYZED"
    assert result["duration_ms"] is None
    assert result["analysis"]["reference_diagnostics"]["duration_ms"] is None
    assert result["analysis"]["marginal_diagnostics"]["duration_ms"] is None
    assert all(x["diagnostics"]["duration_ms"] is None for x in result["analysis"]["sensitivity"])
    assert result["timing_basis"] == "RUNTIME_CLOCK_ELAPSED_NOT_BILLED_CPU"


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


@pytest.mark.parametrize('with_storage', [False, True])
def test_staging_copies_only_allowlisted_sources_and_refuses_existing_directory(tmp_path, monkeypatch, with_storage):
    path = Path("scripts/prepare_python_worker.py").resolve()
    spec = importlib.util.spec_from_file_location("prepare_python_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "stage"
    monkeypatch.setattr(module, "private_root", lambda _: root)
    assert module.prepare("ignored-test", with_storage) == root
    assert {p.relative_to(root / "src").as_posix() for p in (root / "src").rglob("*") if p.is_file()} == {
        "entry.py",
        "hr_platform/__init__.py",
        "hr_platform/model.py",
        "hr_platform/cloud_model.py",
        "hr_platform/cloud_history.py",
        "hr_platform/cloud_race_files.py",
        "hr_platform/cloud_normalization.py",
        "hr_platform/race_files.py",
        "hr_platform/history.py",
        "hr_platform/common.py",
        "hr_platform/parser.py",
        "hr_platform/paper_rules.py",
        "hr_platform/cloud_pages.py",
        "hr_platform/race_state.py",
        "hr_platform/official_payout.py",
        "hr_platform/payout_check.py",
    }
    assert (root / "src/hr_platform/model.py").read_bytes() == Path("src/hr_platform/model.py").read_bytes()
    config = json.loads((root / "wrangler.jsonc").read_text())
    assert not config["workers_dev"] and not config["preview_urls"] and config["triggers"]["crons"] == []
    assert 'services' not in config
    if with_storage:
        actual = json.loads(Path('wrangler.jsonc').read_text())
        assert config['r2_buckets'] == actual['r2_buckets']
        assert config['d1_databases'][0]['database_id'] == actual['d1_databases'][0]['database_id']
        assert json.loads(config['vars']['STORAGE_POLICY_JSON']) == json.loads(
            Path('configs/cloud-storage.json').read_text())
        assert config['limits']['cpu_ms'] == json.loads(Path('configs/cloud-storage.json').read_text())['worker_cpu_ms']
    else:
        assert not {'r2_buckets', 'd1_databases'} & config.keys()
    assert config["python_modules"]["exclude"] == ["**/*.pyc", "**/tests/**", "**/*.pyi"]
    with pytest.raises(ValueError, match="FRESH_BUILD"):
        module.prepare("ignored-test")
