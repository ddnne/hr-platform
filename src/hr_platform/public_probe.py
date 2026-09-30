"""Small, fixed-time public-page qualification. Raw bodies only, never Paper."""

from datetime import datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo
from .common import identity, instant, stamp

FORMAT = "nankankeiba-public-probe-v1"
KIND = "public_odds_probe"


def validate_policy(policy):
    fields = {"version", "interval_seconds", "capture_window_seconds", "before_start_seconds",
              "max_wait_seconds", "sleep_seconds"}
    if not isinstance(policy, dict) or set(policy) != fields or policy["version"] != "nankankeiba-timing-probe-v1":
        raise ValueError("PROBE_POLICY")
    integers = fields - {"version", "before_start_seconds"}
    if (any(type(policy[k]) is not int for k in integers)
        or not 60 <= policy["interval_seconds"] <= 3600
        or not 0 < policy["capture_window_seconds"] <= 10
        or not 0 < policy["max_wait_seconds"] <= 3600
        or not 0 < policy["sleep_seconds"] <= 60):
        raise ValueError("PROBE_POLICY")
    offsets = policy["before_start_seconds"]
    if (not isinstance(offsets, list) or not 2 <= len(offsets) <= 5
        or any(type(x) is not int or not 60 < x <= 3600 for x in offsets)
        or any(a - b < policy["interval_seconds"] for a, b in zip(offsets, offsets[1:]))
        or offsets[-1] - policy["capture_window_seconds"] < 60):
        raise ValueError("PROBE_OFFSETS")


def make_plan(url, scheduled_start_at, policy):
    validate_policy(policy)
    # Only the published, unauthenticated odds-list routes qualified in M0.
    match = re.fullmatch(r"https://www\.nankankeiba\.com/odds/(\d{16})(01|03|04|08|09)\.do", url)
    if not match:
        raise ValueError("PROBE_URL")
    scope = match[1]
    datetime.strptime(scope[:8], "%Y%m%d")
    start = instant(scheduled_start_at)
    if start.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d") != scope[:8]:
        raise ValueError("PROBE_DATE")
    items = []
    for offset in policy["before_start_seconds"]:
        at = start - timedelta(seconds=offset)
        if at.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d") != scope[:8]:
            raise ValueError("PROBE_DATE")
        items.append({"id": f"before-{offset}", "kind": KIND, "url": url, "scope": scope,
                      "at": stamp(at.isoformat()),
                      "until": stamp((at + timedelta(seconds=policy["capture_window_seconds"])).isoformat())})
    return {"format": FORMAT, "policy": dict(policy), "scheduled_start_at": stamp(start.isoformat()),
            "requests": items}


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"format", "policy", "scheduled_start_at", "requests"}:
        raise ValueError("PROBE_PLAN")
    items = plan["requests"]
    if not isinstance(items, list) or not items or not isinstance(items[0], dict) or "url" not in items[0]:
        raise ValueError("PROBE_PLAN")
    if plan != make_plan(items[0]["url"], plan["scheduled_start_at"], plan["policy"]):
        raise ValueError("PROBE_PLAN")


def history(store, plan):
    """Read every planned capture, including repeats/failures and missing slots.

    This is raw collection history, not an as-of view of qualified odds.
    A later parser must record its own parse/availability time.
    """
    validate_plan(plan)
    key = identity(plan)
    if not store.db.execute("SELECT 1 FROM sample_plans WHERE id=?", (key,)).fetchone():
        raise ValueError("PROBE_NOT_REGISTERED")
    rows = []
    for item in plan["requests"]:
        attempt_id = identity([key, item["id"]])
        row = store.db.execute("SELECT receipt_hash FROM sample_requests WHERE id=?", (attempt_id,)).fetchone()
        receipt = json.loads(store.read_body(row[0], "receipts")) if row and row[0] else None
        rows.append({"item_id": item["id"], "scheduled_capture_at": item["at"],
                     "window_end_at": item["until"], "attempt_id": attempt_id if row else None,
                     "status": receipt["outcome"] if receipt else "INCOMPLETE_ATTEMPT" if row else "NOT_ATTEMPTED",
                     "receipt": receipt, "parsed_at": None, "available_at": None})
    return {"plan_id": key, "captures": rows, "raw_only": True, "paper_eligible": False}
