"""The same observation/parse ordering for local and cloud as-of reads."""

from .common import instant, seconds, stamp


def asof_view(histories, markets, at, max_age=300, gaps=None):
    selected = {}
    for market in markets:
        candidates = [
            x for x in histories.get(market, [])
            if x["available_at"] is not None and stamp(x["available_at"]) <= stamp(at)
            and x["event"]["dataset_kind"] != "FINAL_ONLY"
        ]
        if candidates:
            # A later reparse of an older receipt never replaces a newer observation.
            item = dict(max(candidates, key=lambda x: (
                instant(x["received_at"]), instant(x["available_at"]), x["parse_id"],
            )))
            item["age_seconds"] = seconds(at, item["received_at"])
            selected[market] = item
    reason = "DATA_MISSING" if len(selected) != len(markets) else None
    if any(x["age_seconds"] < 0 or x["age_seconds"] > max_age for x in selected.values()):
        reason = "STALE"
    return {"asof_at": stamp(at), "markets": selected, "reason": reason, "gaps": gaps}
