import argparse
import json
from pathlib import Path
import yaml
from . import fixtures as f
from .store import Store
from .paper import decide, settle


def run(root):
    clock = [f.at(0, 2)]
    store = Store(root, clock=lambda: clock[0])
    store.plan([f.at(m) for m in (0, 2, 4, 6)])
    a, b = f.archive(), f.archive(kind="nonuniform")
    for name, minute, raw, status in [
        ("a", 0, a, 200),
        ("a-again", 2, a, 200),
        ("failure", 4, None, 503),
        ("b", 6, b, 200),
    ]:
        clock[0] = f.at(minute, 2)
        store.ingest(f.event(name, minute, status), raw)
    store.ingest(f.event("b", 6), b)  # same delivery
    clock[0] = f.at(8, 2)
    store.reparse("a", "repair-v2")
    view = store.asof(f.RACE, ["win", "exacta", "quinella"], f.at(4))
    assert view["markets"]["quinella"]["observation_id"] == "a-again"
    assert len(store.history(f.RACE, "quinella", f.at(9))) == 4  # three observations plus one reparse
    config = yaml.safe_load(Path("configs/research.yaml").read_text())
    clock[0] = f.at(4, 20)  # explicit SYNTHETIC replay, never a live Paper completion time
    decisions = decide(store, f.RACE, f.schedule(), config)
    clock[0] = f.at(20, 2)
    final_event = f.event("final", 20, kind="FINAL_ONLY")
    final_event["race_states"][f.RACE]["status"] = "FINAL"
    store.ingest(final_event, b)
    settlements = [settle(store, d["id"], f.payout()) for d in decisions]
    result = {
        "dataset_kind": "SYNTHETIC",
        "clock_basis": "fixture_replay_not_live",
        "metrics": store.metrics(),
        "asof_observation": view["markets"]["quinella"]["observation_id"],
        "missing_slots": view["gaps"],
        "decisions": [
            {k: d[k] for k in ("model", "status", "reason", "selection", "stake_yen")} for d in decisions
        ],
        "settlements": settlements,
    }
    (Path(root) / "demo-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    store.close()
    # Verify real disk re-read rather than an in-memory latest cache.
    reopened = Store(root)
    assert reopened.asof(f.RACE, ["quinella"], f.at(4))["markets"]["quinella"]["observation_id"] == "a-again"
    reopened.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="private/demo")
    args = parser.parse_args()
    summary = run(args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
