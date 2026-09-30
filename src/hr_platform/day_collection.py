"""One day's whole NAR odds file through the existing private capture path.

This bounded local runner is for qualification. It does not install a daemon,
unblock the Cloudflare source, infer market finality, or place Paper bets.
"""

from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo
from .common import instant, stamp
from .parser import ARITY
from .race_metadata import MetadataEvidence
from .realdata import completeness
from .sampling import DAY_FORMAT, INTERVAL, ZIP_URLS, advance_plan, run_plan, validate_plan


def validate_settings(config):
    fields = {"interval_seconds", "capture_window_seconds", "max_age_seconds", "max_wait_seconds", "sleep_seconds"}
    if (set(config) != fields | {"version"} or config["version"] != "nar-day-collection-v1"
        or any(type(config[k]) is not int for k in fields)
        or not INTERVAL <= config["interval_seconds"] <= 86400
        or not 0 < config["capture_window_seconds"] <= INTERVAL
        or not 0 < config["max_age_seconds"] <= 86400
        or not 0 < config["max_wait_seconds"] <= 86400
        or not 0 < config["sleep_seconds"] <= 60):
        raise ValueError("DAY_SETTINGS")


def make_plan(start, end, config):
    validate_settings(config)
    start, end = instant(start), instant(end)
    date = start.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")
    if end <= start or end.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d") != date:
        raise ValueError("DAY_PLAN_DATE")
    items, at = [], start
    while at < end:
        items.append({"id": f"odds-{at.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%H%M%S')}",
                      "kind": "odds", "scope": date, "url": ZIP_URLS["odds"], "at": stamp(at.isoformat()),
                      "until": stamp(min(end, at + timedelta(seconds=config["capture_window_seconds"])).isoformat())})
        at += timedelta(seconds=config["interval_seconds"])
    plan = {"format": DAY_FORMAT, "requests": items}
    validate_plan(plan)
    return plan


def step(store, plan, *, samples=None):
    if plan.get("format") != DAY_FORMAT:
        raise ValueError("DAY_PLAN_REQUIRED")
    return advance_plan(store, plan, samples=samples)


def run(store, plan, config, wait_seconds=0, *, samples=None, sleeper=time.sleep, timer=time.monotonic):
    validate_settings(config)
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= config["max_wait_seconds"]:
        raise ValueError("DAY_WAIT_LIMIT")
    if plan.get("format") != DAY_FORMAT:
        raise ValueError("DAY_PLAN_REQUIRED")
    return run_plan(store, plan, wait_seconds, max_wait_seconds=config["max_wait_seconds"],
                    sleep_seconds=config["sleep_seconds"], samples=samples, sleeper=sleeper, timer=timer)


def race_watch(store, date, at, config):
    """Read all saved races at a fixed availability time without HTTP or models."""
    validate_settings(config)
    datetime.strptime(date, "%Y%m%d")
    at = stamp(at)
    metadata = MetadataEvidence(store).asof(date, at)
    evidence = metadata["evidence"]
    entries = evidence.get("races", {}) if evidence else {}
    observed = {r[0] for r in store.db.execute("""SELECT DISTINCT s.race_id FROM snapshots s
        JOIN parses p ON p.id=s.parse_id WHERE s.race_id LIKE ? AND p.available_at<=?""", (date + ":%", at))}
    result = {"date": date, "asof_at": at, "metadata_observation_id": evidence["observation_id"] if evidence else None,
              "metadata_age_seconds": metadata["age_seconds"], "races": [], "paper_eligible": False,
              "freshness_basis": "COLLECTOR_RECEIPT_ONLY", "provider_update_latency_bound_seconds": None}
    for race_id in sorted(observed | entries.keys()):
        view = store.asof(race_id, list(ARITY), at, config["max_age_seconds"])
        markets = {k: v["content"] for k, v in view["markets"].items()}
        coverage = completeness({race_id: {"markets": markets}}, {"races": entries})[race_id]
        observations = {}
        for market, record in view["markets"].items():
            observations[market] = {k: record[k] for k in ("observation_id", "received_at", "available_at", "age_seconds")}
        race = entries.get(race_id, {})
        result["races"].append({"race_id": race_id, "scheduled_start_at": race.get("scheduled_start_at"),
                                "result_present": race.get("result_present"), "discipline": race.get("discipline", "UNKNOWN"),
                                "coverage": coverage, "observations": observations,
                                "stale_markets": [k for k, v in view["markets"].items()
                                                  if v["age_seconds"] > config["max_age_seconds"]],
                                "source_updated_at": None, "paper_eligible": False})
    result["gaps"] = [x for x in store.gaps(at)
                      if instant(x["slot"]).astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d") == date]
    return result
