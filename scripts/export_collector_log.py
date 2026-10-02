"""Read a bounded capture-log window from the designated dev D1, without NAR access."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from hr_platform.cli import private_root
from hr_platform.collector_log import FORMAT, SOURCE, MAX_ROWS, FIELDS, validate_export, window
from hr_platform.common import canonical, instant, stamp, utcnow
from hr_platform.store import Store

DATABASE = "hr-platform-dev-index"
DATABASE_ID = "81fc8e82-cb25-4baa-834d-b69a8f2ac977"


def export(store, start, end, runner=subprocess.run):
    start, end = window(start, end)
    # Numeric bounds avoid timestamp text-format differences (Z vs +00:00) and SQL injection.
    lower, upper = instant(start).timestamp(), instant(end).timestamp()
    statement = (
        f"SELECT {','.join(FIELDS)} FROM captures "
        f"WHERE event_id LIKE '{SOURCE}:%' "
        f"AND status NOT IN ('SYNTHETIC_FIXTURE','IMPORTED_RAW_STORED') "
        f"AND unixepoch(scheduled_capture_at,'subsec')>={lower} "
        f"AND unixepoch(scheduled_capture_at,'subsec')<{upper} "
        f"ORDER BY scheduled_capture_at,event_id LIMIT {MAX_ROWS + 1}"
    )
    read_start = stamp(store.clock())
    if end > read_start:
        raise ValueError("FUTURE_LOG_WINDOW")
    started = time.perf_counter()
    result = runner(
        [
            "node_modules/.bin/wrangler",
            "d1",
            "execute",
            DATABASE,
            "--config",
            "wrangler.jsonc",
            "--remote",
            "--command",
            statement,
            "--json",
        ],
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    read_end = stamp(store.clock())
    # Wrangler responses/errors may contain private metadata; never forward them to stdout.
    store.body(result.stdout, "reports")
    store.body(result.stderr, "reports")
    if result.returncode:
        raise ValueError("D1_READ_FAILED")
    response = json.loads(result.stdout)
    if not isinstance(response, list) or len(response) != 1 or response[0].get("success") is not True:
        raise ValueError("D1_RESPONSE")
    data = {
        "format": FORMAT,
        "source": SOURCE,
        "read_started_at": read_start,
        "read_completed_at": read_end,
        "range_start": start,
        "range_end": end,
        "rows": response[0]["results"],
    }
    validate_export(data, stamp(store.clock()))
    digest = store.body(canonical(data), "receipts")
    metrics = {
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "remote_commands": 1,
        "export_rows": len(data["rows"]),
        "nar_requests": 0,
        "d1_meta": response[0].get("meta"),
        "export_hash": digest,
    }
    metric_hash = store.body(canonical(metrics), "reports")
    return {
        "status": "LOG_EXPORTED",
        "private_export": str(store.root / "receipts" / digest),
        "private_report": str(store.root / "reports" / metric_hash),
    }


def main(argv=None):
    if any(os.environ.get(k, "").lower() in {"true", "1"} for k in ("CI", "GITHUB_ACTIONS")):
        print('{"status":"REMOTE_EXPORT_DISABLED_IN_CI"}', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--until", dest="end", required=True)
    parser.add_argument("--root", default="private/collector-exports")
    args = parser.parse_args(argv)
    store = None
    mask = os.umask(0o077)
    try:
        config = json.loads(Path("wrangler.jsonc").read_text())
        expected = {
            "binding": "INDEX",
            "database_name": DATABASE,
            "database_id": DATABASE_ID,
            "migrations_dir": "migrations",
        }
        if config.get("name") != "hr-platform-dev-ingestion" or config.get("d1_databases") != [expected]:
            raise ValueError("DEV_DATABASE_REQUIRED")
        store = Store(private_root(args.root), clock=utcnow)
        print(json.dumps(export(store, args.start, args.end)))
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.SubprocessError):
        print('{"status":"LOG_EXPORT_FAILED"}', file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
        os.umask(mask)


if __name__ == "__main__":
    raise SystemExit(main())
