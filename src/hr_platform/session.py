"""One finite, prospective Paper experiment using existing capture and ledger code.

A step never sleeps or schedules itself. An optional short foreground run waits
for the next planned action; it does not install a recurring collector.
"""

import time
from .common import identity, seconds, stamp
from .official_payout import PayoutEvidence
from .prospective import SCHEMA, current_plan, tick
from .sampling import Samples, validate_plan


def step(store, paper_plan_id, sample_plan, *, samples=None):
    store.db.executescript(SCHEMA)
    plan = current_plan(store, paper_plan_id)
    if not plan:
        raise ValueError("PROSPECTIVE_PLAN_REQUIRED")
    sample_id = validate_plan(sample_plan)
    items = sample_plan["requests"]
    # Bind every resource to the enrolled race; do not silently substitute a
    # race with better coverage or a different outcome after seeing prices.
    if ({x["kind"] for x in items} != {"odds", "race", "state", "payout"}
        or sum(x["kind"] == "odds" for x in items) < 2
        or any(x["scope"] != (plan["race_id"].split(":")[0] if x["kind"] in {"odds", "race"}
                              else plan["race_id"]) for x in items)):
        raise ValueError("SESSION_CAPTURE_SCOPE")
    samples = samples or Samples(store)
    samples.register(sample_plan)
    report = {"session_id": identity([paper_plan_id, sample_id]), "paper_plan_id": paper_plan_id,
              "sample_plan_id": sample_id, "captures": [], "decisions": [], "settlements": [], "errors": []}
    next_actions = []
    # Capture remains independent of a later model/settlement failure. Each
    # source request is reserved/idempotent inside Samples, across restarts too.
    for item in items:
        try:
            result = samples.capture(sample_plan, item["id"])
        except Exception as exc:
            result = {"status": "CAPTURE_ERROR", "error_class": type(exc).__name__}
        report["captures"].append({"item_id": item["id"], **result})
        if result["status"] == "WAIT" and result["next_at"] < stamp(item["until"]):
            next_actions.append((result["next_at"], stamp(item["until"])))
    try:
        paper = tick(store, paper_plan_id)
        report["decisions"] = paper["decisions"]
        plan = current_plan(store, paper_plan_id)
        for decision in paper["decisions"]:
            if decision["status"] == "NOT_DUE":
                next_actions.append((decision["asof_at"], None))
    except Exception as exc:
        report["errors"].append({"stage": "decision", "error_class": type(exc).__name__})
    evidence = None
    try:
        payouts = PayoutEvidence(store)
        evidence = payouts.asof(plan["race_id"], store.clock())["evidence"]
    except Exception as exc:
        report["errors"].append({"stage": "payout", "error_class": type(exc).__name__})
    report["payout_evidence_id"] = evidence["id"] if evidence else None
    if evidence and evidence["status"] == "PAYOUT_QUALIFIED":
        for decision in report["decisions"]:
            if decision["status"] not in {"PAPER_BET", "NO_BET"}:
                continue
            try:
                report["settlements"].append(payouts.settle_decision(decision["id"], evidence["id"]))
            except Exception as exc:
                report["errors"].append({"stage": "settlement", "decision_id": decision["id"],
                                         "error_class": type(exc).__name__})
    # Do not busy-retry an error or extend an expired capture window. The caller
    # receives the next future action; a missed window remains missing.
    now = stamp(store.clock())
    report["next_at"] = min((max(t, now) for t, until in next_actions
                             if until is None or now < until), default=None)
    fixed = [d for d in report["decisions"] if d["status"] in {"PAPER_BET", "NO_BET"}]
    settled = {s["decision_id"] for s in report["settlements"] if s["status"] in {"SETTLED", "NO_BET"}}
    complete = len(fixed) == len(plan["config"]["comparison_models"]) and all(d["id"] in settled for d in fixed)
    report["status"] = "WAITING" if report["next_at"] else "COMPLETE" if complete else "INCOMPLETE"
    return report


def run(store, paper_plan_id, sample_plan, wait_seconds=0, *, samples=None, sleeper=time.sleep, timer=time.monotonic):
    """Advance once, optionally waiting within a bounded foreground window."""
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 900:
        raise ValueError("SESSION_WAIT_LIMIT")
    end = timer() + wait_seconds
    while True:
        report = step(store, paper_plan_id, sample_plan, samples=samples)
        if report["next_at"] is None:
            return report
        while True:
            delay = seconds(report["next_at"], store.clock())
            if timer() >= end or delay > end - timer():
                return report
            if delay <= 0:
                break
            sleeper(min(delay, 30))
