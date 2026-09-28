# hr-platform

地方競馬の平地オッズ断面を自前で保存し、市場インプライドの上位3着同時分布を研究するPaper専用基盤。
正本は [docs/project_brief.md](docs/project_brief.md)（統合v0.4）。実装・検証状況は [docs/status.md](docs/status.md)。

## ローカル実行

Python 3.12、uv、Node.js 22以上、npmを使用。

```sh
make setup
make test
make demo
```

`make demo` は通信しない合成fixtureで、ZIP入力→原本・観測履歴→as-of→Qref/Qmarg・依存診断→Paper判断→払戻精算を実行する。出力とSQLite台帳はGit対象外の `private/demo/`。同じコマンドを再実行しても観測・購入・精算は増えない。架空時計による再現であり、実時間Paperではない。

初期の主対象は馬連、参照は単勝＋馬単。対象券種を校正から外し、同一周辺分布・馬単からの直接集計を同じ競走・同じas-ofで比較する。Qは市場参照分布であり、実証済みの現実確率や利益ではない。

## 保存と公開境界

- `Store.history`、`Store.latest`、`Store.asof`を分離。as-ofは利用可能時刻、鮮度、元観測、欠測を保持。
- 実データ・原本・台帳・詳細出力・SecretsをGitや公開CIへ置かない。通常テストは合成データだけを利用する。
- `.gitignore`に加え、`uv run python scripts/check_public_boundary.py`で追跡対象を検査する。
- 実投票の接続・認証情報は持たない。Windows、購入データ、quant-platformへの統合は使用しない。

## Cloudflare

収集WorkerのR2/D1保存・拒否時停止・待機をローカルworkerdで試験する。外向き通信はテスト内で置換する。公開HTTPは404。

```sh
npm run typegen
npm run typecheck
npm test
npm run build    # dry-runのみ
```

現状はCron・収集スイッチとも無効。提供元条件と対象Cloudflare環境の承認、実リソースへのbinding設定が必要。実稼働、実ZIP互換性、継続収集、収益性は未確認。
