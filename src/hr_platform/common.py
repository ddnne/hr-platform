from datetime import datetime, timedelta, timezone
import hashlib
import json


MODEL_PROBABILITY_FIELDS = {"reference": "p_ref", "marginal": "p_marg", "direct": "p_direct"}
# Official contemporary NAR schedules use JST, without a runtime tzdata dependency.
JST = timezone(timedelta(hours=9))


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def stamp(value):
    return instant(value).isoformat(timespec="microseconds")


def seconds(later, earlier):
    return (instant(later) - instant(earlier)).total_seconds()


def paper_asof(schedule, config, race_id=None):
    """One configured cutoff for enrollment, Paper and retrospective diagnostics."""
    offset = config["asof_before_start_seconds"]
    overrides = config.get("asof_before_start_seconds_by_venue", {})
    if overrides:
        if not race_id or len(race_id.split(":")) != 3:
            raise ValueError("TIMING_RACE_ID_REQUIRED")
        offset = overrides.get(race_id.split(":")[1], offset)
    return stamp((instant(schedule["scheduled_start_at"]) - timedelta(seconds=offset)).isoformat())


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return sha(canonical(value))
