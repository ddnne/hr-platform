"""Explicit dev-only storage probe. Synthetic bytes only; no NAR request or Paper row.

Run from the repository root with --execute. Wrangler output is retained privately.
This verifies remote D1/R2 I/O via the CLI, not scheduled Worker execution.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from hr_platform.cli import private_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.parse_args()
    if any(os.environ.get(k, "").lower() in {"true", "1"} for k in ("CI", "GITHUB_ACTIONS")):
        raise SystemExit("Cloud probe is disabled in CI.")
    config = json.loads(Path("wrangler.jsonc").read_text())
    if (
        config["name"] != "hr-platform-dev-ingestion"
        or config["vars"]
        != {"COLLECTION_ENABLED": "false", "SOURCE_APPROVED": "false", "CAPTURE_SLOTS_JSON": "[]", "DAILY_COLLECTION_ENABLED": "false"}
        or config["workers_dev"]
        or config["preview_urls"]
        or config["triggers"]["crons"]
        or config["d1_databases"][0]["database_name"] != "hr-platform-dev-index"
        or config["r2_buckets"][0]["bucket_name"] != "hr-platform-dev-private"
    ):
        raise SystemExit("Dev configuration must remain disabled.")
    os.umask(0o077)
    run_id = str(uuid.uuid4())
    root = private_root(f"private/cloud-probes/{run_id}")
    root.mkdir(parents=True)
    records = []
    started = time.perf_counter()

    def command(*args, expect_json=False):
        index = len(records)
        t = time.perf_counter()
        result = subprocess.run(["node_modules/.bin/wrangler", *args], capture_output=True, timeout=60)
        (root / f"command-{index}.stdout").write_bytes(result.stdout)
        (root / f"command-{index}.stderr").write_bytes(result.stderr)
        records.append(
            {
                "operation": list(args[:3]),
                "elapsed_ms": (time.perf_counter() - t) * 1000,
                "success": result.returncode == 0,
            }
        )
        if result.returncode:
            raise RuntimeError("REMOTE_COMMAND_FAILED")
        return json.loads(result.stdout) if expect_json else None

    def sql(statement):
        return command(
            "d1",
            "execute",
            "hr-platform-dev-index",
            "--remote",
            "--command",
            statement,
            "--json",
            expect_json=True,
        )

    payload = b'{"kind":"SYNTHETIC_STORAGE_PROBE","version":1}'
    digest = hashlib.sha256(payload).hexdigest()
    (root / "payload.bin").write_bytes(payload)
    prefix = f"development-probes/{run_id}"
    bucket = "hr-platform-dev-private"
    report = {
        "run_id": run_id,
        "kind": "SYNTHETIC_STORAGE_PROBE",
        "nar_requests": 0,
        "scheduled_worker_execution": False,
        "raw_bytes": len(payload),
        "raw_objects": 1,
    }
    try:
        command(
            "r2",
            "object",
            "put",
            f"{bucket}/{prefix}/raw/{digest}",
            "--remote",
            "--file",
            str(root / "payload.bin"),
        )
        sql(
            "CREATE TABLE IF NOT EXISTS development_probes (run_id TEXT, observation_id TEXT PRIMARY KEY, observed_at TEXT, raw_sha256 TEXT, kind TEXT)"
        )
        for i in range(2):
            observed = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            observation_id = f"{run_id}-{i}"
            receipt = {
                "observation_id": observation_id,
                "observed_at": observed,
                "raw_sha256": digest,
                "kind": "SYNTHETIC_STORAGE_PROBE",
            }
            local = root / f"observation-{i}.json"
            local.write_text(json.dumps(receipt))
            command(
                "r2",
                "object",
                "put",
                f"{bucket}/{prefix}/observations/{i}.json",
                "--remote",
                "--file",
                str(local),
            )
            sql(
                f"INSERT INTO development_probes VALUES ('{run_id}','{observation_id}','{observed}','{digest}','SYNTHETIC_STORAGE_PROBE')"
            )
        result = sql(
            f"SELECT observation_id,observed_at,raw_sha256 FROM development_probes WHERE run_id='{run_id}' ORDER BY observed_at"
        )
        rows = result[0]["results"]
        if (
            len(rows) != 2
            or len({r["observed_at"] for r in rows}) != 2
            or {r["raw_sha256"] for r in rows} != {digest}
        ):
            raise RuntimeError("REMOTE_HISTORY_MISMATCH")
        command(
            "r2",
            "object",
            "get",
            f"{bucket}/{prefix}/raw/{digest}",
            "--remote",
            "--file",
            str(root / "readback.bin"),
        )
        if (root / "readback.bin").read_bytes() != payload:
            raise RuntimeError("REMOTE_BODY_MISMATCH")
        for i in range(2):
            destination = root / f"readback-{i}.json"
            command(
                "r2",
                "object",
                "get",
                f"{bucket}/{prefix}/observations/{i}.json",
                "--remote",
                "--file",
                str(destination),
            )
            if json.loads(destination.read_text())["observation_id"] != rows[i]["observation_id"]:
                raise RuntimeError("REMOTE_RECEIPT_MISMATCH")
        report.update(status="PASSED", observations=2, history_readback=True)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        report["status"] = "FAILED"
    finally:
        report.update(commands=records, total_elapsed_ms=(time.perf_counter() - started) * 1000)
        (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"status": report["status"], "private_report": str(root / "report.json")}))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
