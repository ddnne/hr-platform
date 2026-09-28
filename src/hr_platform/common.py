from datetime import datetime, timezone
import hashlib
import json


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


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return sha(canonical(value))
