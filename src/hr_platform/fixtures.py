"""Generated synthetic data only. Not observations of any real NAR race."""

import csv
import io
import zipfile
from datetime import timedelta
import numpy as np
from .common import instant
from .model import states, matrix, key
from .parser import HEADERS

RACE = "20000101:SYNTHETIC:1"
BASE = "2000-01-01T05:00:00+00:00"  # 14:00 JST, intentionally historical synthetic clock


def at(minutes=0, seconds=0):
    return (instant(BASE) + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def state():
    return {
        "race_id": RACE,
        "venue": "SYNTHETIC",
        "runners": [1, 2, 3, 4],
        "runner_version": "r1",
        "discipline": "FLAT",
        "surface": "DIRT",
        "status": "PRE_RACE",
        "pre_race_evidence": "synthetic",
        "known_at": at(-30),
        "schedule_version": "schedule-v1",
    }


def schedule():
    return {
        "version": "schedule-v1",
        "known_at": at(-30),
        "scheduled_start_at": at(14),
        "sales_close_at": None,
    }


def distribution(kind="mixture"):
    omega = states([1, 2, 3, 4])
    u = np.ones(len(omega)) / len(omega)
    if kind == "uniform":
        return u
    if kind == "nonuniform":
        q = np.array([i + 1 for i in range(len(omega))], dtype=float)
        return q / q.sum()
    b = np.array([1 / 8 if set(s[:2]) in ({1, 2}, {3, 4}) else 0 for s in omega])
    return 0.8 * u + 0.2 * b


def markets(kind="mixture", distorted=False, rounded=False):
    omega = states([1, 2, 3, 4])
    q = distribution(kind)
    result = {}
    for market in ("win", "exacta", "quinella", "trio", "trifecta"):
        selections, a = matrix(omega, market)
        prices = 0.8 / (a @ q)
        result[market] = {"source_updated_at": None, "quotes": {}}
        for s, o in zip(selections, prices):
            if distorted and market == "quinella" and s == (1, 2):
                o *= 2
            if rounded:
                o = np.floor(o * 10) / 10
            result[market]["quotes"][key(s)] = {"odds": float(o), "odds_max": None, "display_status": "FIXED"}
    return result


def archive(kind="mixture", distorted=True, rounded=False, extra=None):
    labels = {"win": "単勝", "exacta": "馬単", "quinella": "馬複", "trio": "３連複", "trifecta": "３連単"}
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(HEADERS)
    for market, data in markets(kind, distorted, rounded).items():
        for k, quote in data["quotes"].items():
            selection = k.split("-")
            writer.writerow(
                [
                    "SYNTHETIC",
                    "20000101",
                    1,
                    labels[market],
                    *selection,
                    *([""] * (3 - len(selection))),
                    quote["odds"],
                    "",
                    "",
                ]
            )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        entry = zipfile.ZipInfo("20000101_odds.csv", (2000, 1, 1, 0, 0, 0))
        z.writestr(entry, text.getvalue().encode("utf-8-sig"))
        if extra:
            z.writestr(*extra)
    return out.getvalue()


def event(name, minute, status=200, kind="SYNTHETIC", **overrides):
    e = {
        "attempt_id": name,
        "scheduled_capture_at": at(minute),
        "fetch_started_at": at(minute),
        "collector_received_at": at(minute, 1),
        "ingest_received_at": at(minute, 1),
        "status": status,
        "dataset_kind": kind,
        "availability_basis": "assumed" if kind == "SYNTHETIC" else "observed",
        "file_timestamp": at(minute),
        "source_published_at": None,
        "source_updated_at": None,
        "source": "synthetic://local",
        "retry_of": None,
        "race_states": {RACE: state()},
        "etag": name,
    }
    e.update(overrides)
    return e


def payout(revision="official-fixture-v1", **overrides):
    p = {
        "race_id": RACE,
        "revision": revision,
        "source_kind": "SYNTHETIC",
        "source_reference": "synthetic://official-payout",
        "available_at": at(20),
        "final": True,
        "complete_markets": ["quinella"],
        "void": False,
        "tickets": [{"market": "quinella", "selection": "1-2", "payout_per_100": 650, "refund_per_100": 0}],
    }
    p.update(overrides)
    return p
