"""Private data CLI. Finite provider requests use the shared sampling module."""

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
        description="実データの非公開処理。collect-sample/paper-sessionは事前計画した少数の通信を行う。"
    )
    parser.add_argument("--root", default="private/real", help="Git対象外のprivate配下の保存先")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("collect-sample", help="少数取得計画の1項目を実行・保存。待機/再配送では通信しない")
    sample.add_argument("--plan", required=True, help="事前に保存した有限取得計画JSON")
    sample.add_argument("--item", required=True)
    session = commands.add_parser("paper-session", help="有限取得・固定Paper判断・公式精算を進める。常時起動はしない")
    session.add_argument("--paper-plan", required=True)
    session.add_argument("--sample-plan", required=True)
    session.add_argument("--wait-seconds", type=int, default=0, help="次の枠まで待機できる時間。既定0、最大900秒")
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
    payout = commands.add_parser("check-payout", help="保存済み成績HTMLとレースCSVの払戻を照合。精算なし")
    payout.add_argument("--race-zip", required=True)
    payout.add_argument("--html", required=True)
    payout.add_argument("--race", required=True)
    payout.add_argument("--encoding", choices=["utf-8-sig", "cp932"], required=True)
    payout_import = commands.add_parser("import-payout", help="公式成績HTMLから馬連・三連複の払戻と除外返還を保存")
    payout_import.add_argument("--html", required=True)
    payout_import.add_argument("--receipt", required=True)
    payout_import.add_argument("--race", required=True)
    payout_settle = commands.add_parser("settle-payout", help="保存済み公式払戻で既存のPaper判断を精算")
    payout_settle.add_argument("--decision", required=True)
    payout_settle.add_argument("--evidence", required=True)
    for name in ("payout-asof", "payout-history"):
        payout_query = commands.add_parser(name, help="公式払戻の利用可能時点と解析履歴を読戻し")
        payout_query.add_argument("--race", required=True)
        payout_query.add_argument("--at", required=True)
    state = commands.add_parser("import-state", help="公式単複ページの状態表示と取得記録を非公開保存")
    state.add_argument("--html", required=True)
    state.add_argument("--receipt", required=True)
    state.add_argument("--race", required=True)
    state_query = commands.add_parser("state-asof", help="その時点で解析済みだった公式状態表示を読戻し")
    state_query.add_argument("--race", required=True)
    state_query.add_argument("--at", required=True)
    state_history = commands.add_parser("state-history", help="利用可能だった公式状態表示の全観測・解析版を読戻し")
    state_history.add_argument("--race", required=True)
    state_history.add_argument("--at", required=True)
    metadata = commands.add_parser("import-metadata", help="取得記録付きの当日レースZIPを一度保存し競走別に読出し")
    metadata.add_argument("--zip", required=True)
    metadata.add_argument("--receipt", required=True)
    metadata.add_argument("--date", required=True, help="YYYYMMDD")
    for name in ("metadata-asof", "metadata-history"):
        query = commands.add_parser(name, help="競走情報の利用可能時点と全解析履歴を読戻し")
        query.add_argument("--date", required=True)
        query.add_argument("--at", required=True)
    plan = commands.add_parser("paper-plan", help="将来の競走・判断時刻・研究仮定を事前固定。取得や購入はしない")
    plan.add_argument("--race", required=True)
    plan.add_argument("--config", default="configs/research.yaml")
    tick = commands.add_parser("paper-tick", help="事前固定した判断を一度実施。基準時刻前はNOT_DUE")
    tick.add_argument("--plan", required=True)
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
    if args.command == "paper-session":
        from .session import run as advance

        report = advance(store, args.paper_plan, json.loads(read_limited(args.sample_plan, 64 * 1024)), args.wait_seconds)
    elif args.command == "collect-sample":
        from .sampling import Samples

        report = Samples(store).capture(json.loads(read_limited(args.plan, 64 * 1024)), args.item)
    elif args.command == "inspect":
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
    elif args.command == "check-payout":
        from .payout_check import crosscheck

        report = crosscheck(store, read_limited(args.race_zip), Path(args.race_zip).name,
                            read_limited(args.html, 2 * 1024 * 1024), args.race, args.encoding)
    elif args.command == "import-capture":
        manifest = json.loads(read_limited(args.manifest, 64 * 1024))
        parse_id = adapter.import_capture(manifest, read_limited(args.zip), args.encoding)
        report = dict(store.db.execute("SELECT * FROM parses WHERE id=?", (parse_id,)).fetchone())
    elif args.command in {"import-payout", "settle-payout", "payout-asof", "payout-history"}:
        from .official_payout import PayoutEvidence

        payouts = PayoutEvidence(store)
        if args.command == "import-payout":
            report = payouts.ingest(json.loads(read_limited(args.receipt, 64 * 1024)),
                                    read_limited(args.html, 2 * 1024 * 1024), args.race)
        elif args.command == "settle-payout":
            report = payouts.settle_decision(args.decision, args.evidence)
        elif args.command == "payout-history":
            report = payouts.history(args.race, args.at)
        else:
            report = payouts.asof(args.race, args.at)
    elif args.command in {"import-state", "state-asof", "state-history"}:
        from .race_state import StateEvidence

        states = StateEvidence(store)
        if args.command == "import-state":
            report = states.ingest(json.loads(read_limited(args.receipt, 64 * 1024)),
                                   read_limited(args.html, 2 * 1024 * 1024), args.race)
        elif args.command == "state-history":
            report = states.history(args.race, args.at)
        else:
            report = states.asof(args.race, args.at)
    elif args.command in {"import-metadata", "metadata-asof", "metadata-history"}:
        from .race_metadata import MetadataEvidence

        metadata = MetadataEvidence(store)
        if args.command == "import-metadata":
            report = metadata.ingest(json.loads(read_limited(args.receipt, 64 * 1024)),
                                     read_limited(args.zip), args.date)
        elif args.command == "metadata-history":
            report = metadata.history(args.date, args.at)
        else:
            report = metadata.asof(args.date, args.at)
    elif args.command in {"paper-plan", "paper-tick"}:
        from .prospective import enroll, tick

        if args.command == "paper-plan":
            report = enroll(store, args.race, yaml.safe_load(read_limited(args.config, 64 * 1024)))
        else:
            report = tick(store, args.plan)
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
        return 1 if result["status"] in {
            "QUARANTINED", "ERROR", "NO_COMPATIBLE_RACE", "MODEL_ERROR", "MISMATCH", "REFERENCE_INCONSISTENT"
        } else 0
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
