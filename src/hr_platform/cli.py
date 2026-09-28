"""Offline CLI. Real payloads and query results stay below the ignored private root."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import yaml
from .common import canonical
from .collector_log import CaptureLog
from .parser import parse_odds, VERSION
from .realdata import RealData, completeness, read_limited
from .store import Store


def private_root(value):
    repo = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    requested = Path(value).absolute()
    # Fail before writing if a symlink redirects any private directory outside Git's ignored boundary.
    if any(p.is_symlink() for p in [requested, *requested.parents]):
        raise ValueError("PRIVATE_ROOT_SYMLINK")
    root = requested.resolve()
    if not root.is_relative_to(repo / "private") or root == repo / "private":
        raise ValueError("PRIVATE_ROOT_REQUIRED")
    relative = root.relative_to(repo).as_posix()
    ignored = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", relative + "/probe"], cwd=repo)
    tracked = subprocess.check_output(["git", "ls-files", "--", relative], cwd=repo)
    if ignored.returncode != 0 or tracked:
        raise ValueError("PRIVATE_ROOT_NOT_IGNORED")
    return root


def arguments():
    parser = argparse.ArgumentParser(
        description="実ZIPの非公開検査・取得記録の取込み。ネットワーク通信なし。"
    )
    parser.add_argument("--root", default="private/real", help="Git対象外のprivate配下の保存先")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="取得時刻不明のZIPを検査。観測履歴へは追加しない")
    inspect.add_argument("--zip", required=True)
    inspect.add_argument("--kind", choices=["DAILY_SNAPSHOT", "FINAL_ONLY"], required=True)
    inspect.add_argument("--encoding", choices=["utf-8-sig", "cp932"], required=True)
    inspect.add_argument("--race-zip", help="買い目被覆の比較に使う同日レースZIP。出走状態は未確定のまま")
    capture = commands.add_parser("import-capture", help="完了したWorker manifestと原本を取込み")
    capture.add_argument("--manifest", required=True)
    capture.add_argument("--zip", required=True)
    capture.add_argument("--encoding", choices=["utf-8-sig", "cp932"], required=True)
    diagnostic = commands.add_parser("diagnose", help="一競走の馬連・依存構造を静的比較。Paper台帳は作らない")
    diagnostic.add_argument("--zip", required=True)
    diagnostic.add_argument("--race-zip", required=True)
    diagnostic.add_argument("--kind", choices=["DAILY_SNAPSHOT"], required=True)
    diagnostic.add_argument("--encoding", choices=["utf-8-sig", "cp932"], required=True)
    diagnostic.add_argument("--race", help="未指定なら完全性条件を満たす最初の平地競走")
    diagnostic.add_argument("--config", default="configs/research.yaml")
    log = commands.add_parser("import-log", help="D1の成功・失敗・待機ログを非公開で取込み")
    log.add_argument("--file", required=True)
    log_query = commands.add_parser("capture-log", help="取得状態の履歴を時点指定で再読出し")
    log_query.add_argument("--from", dest="start", required=True)
    log_query.add_argument("--until", dest="end", required=True)
    log_query.add_argument("--at", required=True)
    research = commands.add_parser("research-asof", help="予定発走時刻から固定した断面と推移を診断。購入なし")
    research.add_argument("--race", required=True)
    research.add_argument("--schedule", required=True, help="事前に判明した予定のJSON")
    research.add_argument("--config", default="configs/research.yaml")
    comparison = commands.add_parser("compare-paper", help="同じ競走集合で保存済みPaper台帳を比較")
    comparison.add_argument("--config", default="configs/research.yaml")
    comparison.add_argument("--at", required=True)
    for name in ("history", "asof", "trajectory"):
        query = commands.add_parser(name, help="結果を非公開レポートへ保存")
        query.add_argument("--race", required=True)
        query.add_argument("--market", action="append", required=True)
        query.add_argument("--at", required=name != "history")
    reparse = commands.add_parser(
        "reparse", help="保存原本を現在時刻で再解析。過去の利用可能時刻は変更しない"
    )
    reparse.add_argument("--observation", required=True)
    reparse.add_argument("--version", required=True, help="修正ごとに新しい解析版を指定")
    reparse.add_argument("--encoding", choices=["utf-8-sig", "cp932"], required=True)
    commands.add_parser("metrics", help="保存件数・サイズ・解析時間の集計")
    return parser


def run(args, store):
    adapter = RealData(store)
    if args.command == "inspect":
        report = adapter.inspect(read_limited(args.zip), Path(args.zip).name, args.kind, args.encoding)
        if args.race_zip and report.get("content_type") == "odds":
            race = adapter.inspect(
                read_limited(args.race_zip), Path(args.race_zip).name, args.kind, args.encoding
            )
            if race.get("content_type") != "race" or race["status"] != "PARSED_UNQUALIFIED":
                raise ValueError("RACE_BUNDLE_UNQUALIFIED")
            report = {
                **report,
                "race_inspection_id": race["id"],
                "coverage": completeness(report["content"], race["content"]),
            }
    elif args.command == "diagnose":
        from .diagnostic import diagnose

        report = diagnose(
            store,
            read_limited(args.zip),
            Path(args.zip).name,
            read_limited(args.race_zip),
            Path(args.race_zip).name,
            args.kind,
            args.encoding,
            yaml.safe_load(read_limited(args.config, 64 * 1024)),
            args.race,
        )
    elif args.command == "import-capture":
        manifest = json.loads(read_limited(args.manifest, 64 * 1024))
        parse_id = adapter.import_capture(manifest, read_limited(args.zip), args.encoding)
        report = dict(store.db.execute("SELECT * FROM parses WHERE id=?", (parse_id,)).fetchone())
    elif args.command == "import-log":
        report = CaptureLog(store).ingest(json.loads(read_limited(args.file)))
    elif args.command == "capture-log":
        report = CaptureLog(store).history(args.start, args.end, args.at)
    elif args.command == "history":
        report = {"history": {h: store.history(args.race, h, args.at) for h in args.market}}
    elif args.command == "trajectory":
        from .research import trajectory

        report = trajectory(store, args.race, args.market, args.at)
    elif args.command == "research-asof":
        from .research import research_asof

        report = research_asof(
            store, args.race, json.loads(read_limited(args.schedule, 64 * 1024)),
            yaml.safe_load(read_limited(args.config, 64 * 1024)),
        )
    elif args.command == "compare-paper":
        from .evaluation import compare

        report = compare(store, yaml.safe_load(read_limited(args.config, 64 * 1024)), args.at)
    elif args.command == "asof":
        report = store.asof(args.race, args.market, args.at)
    elif args.command == "reparse":
        version = f"{VERSION}:{args.version}:{args.encoding}"

        def parser(raw, states, _):
            return parse_odds(raw, states, args.encoding)

        parse_id = store.reparse(args.observation, version, parser)
        report = dict(store.db.execute("SELECT * FROM parses WHERE id=?", (parse_id,)).fetchone())
    else:
        report = store.metrics()
    digest = store.body(canonical(report), "reports")
    return {
        "status": report.get("status", "REPORT_SAVED"),
        "private_report": str(store.root / "reports" / digest),
    }


def main(argv=None):
    # No real-file handling in public CI, even if an input is accidentally configured.
    if any(os.environ.get(k, "").lower() in {"true", "1"} for k in ("CI", "GITHUB_ACTIONS")):
        print('{"status":"REAL_DATA_DISABLED_IN_CI"}', file=sys.stderr)
        return 2
    args = arguments().parse_args(argv)
    previous_mask = os.umask(0o077)
    store = None
    try:
        root = private_root(args.root)
        store = Store(root)
        result = run(args, store)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] in {"QUARANTINED", "ERROR", "NO_COMPATIBLE_RACE", "MODEL_ERROR"} else 0
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OSError,
        yaml.YAMLError,
        subprocess.SubprocessError,
    ):
        # No traceback, source cell, filename, receipt, odds or credentials on stdout/stderr.
        print(
            '{"status":"INPUT_OR_STORAGE_ERROR","detail":"入力形式・保存先・先行する200取得記録を確認してください"}',
            file=sys.stderr,
        )
        return 2
    finally:
        if store is not None:
            store.close()
        os.umask(previous_mask)


if __name__ == "__main__":
    raise SystemExit(main())
