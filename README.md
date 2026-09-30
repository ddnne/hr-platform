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

## 実ファイルを使う準備

事前計画した少数サンプルの取得、取得済みZIPの非公開検査、Worker取得記録付き原本の取込み、履歴/as-of/再解析をCLIで実行できる。ローカルの提供元アクセスは共通の有限取得処理へ集約し、同じ保存先で間隔・停止状態を共有する。`collect-sample`は一枠の取得、`paper-session`は有限計画の取得・固定Paper判断・公式精算を進める。D1の成功・失敗・待機ログも非公開で取込み、時点指定で再読出しできる。手順と残条件は [docs/real_data_runbook.md](docs/real_data_runbook.md)。取得時刻不明のZIPは過去の観測にせず、出走状態・返還の意味が未確認のデータではPaper判断・精算を止める。

まずはZIP一組の静的診断を優先する。`diagnose`で馬連の価格差・依存構造・単純換算との差を調べ、有望なら複数競走やPaperへ進む。運用・バックアップの高度化は研究開始の条件にしない。

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

専用devの非公開R2/D1を使用。NAR公式の取得Workerは2回の404を受けて停止中。Mac取得原本のクラウド解析・履歴/as-ofと、有限の実時計Paperは実測済み。件数と制約は [docs/status.md](docs/status.md) に記録する。

別の `hr-platform-dev-archive` が第三者の公開JSONを低頻度で取得し、過去履歴をR2/D1へ保存する。Macの常時起動やモデルの成否に依存しない。初期範囲は過去7日で、完了ページを除き、毎日新しい前日分を追加する。最終値と中間時刻を区別し、過去の利用可能時刻が不明なデータを実行可能なPaper入力には使わない。収益性は未検証。

### 保存済み中間オッズの時点診断

`uv run python -m hr_platform.cli --root private/real research-asof --race RACE_ID --schedule private/inbox/schedule.json`

予定JSONは `version`, `known_at`, `scheduled_start_at`, `sales_close_at`（不明ならnull）を含めます。
予定の出典と事前に判明していた時刻は別途確認が必要です。設定ファイルの発走10分前を固定し、
当時利用可能だった断面だけで三つのモデルを比較します。過去の購入や実行可能性・利益の証明は作りません。
不適格な入力は理由を保存し、モデルを実行しません。

`uv run python -m hr_platform.cli --root private/real trajectory --race RACE_ID --market quinella --at 2026-09-28T05:00:00Z`

推移は実観測のみを返し、同値の再観測を保持します。再解析はその時点までに利用可能な版を選び、
欠測を補間しません。確定済み・レース前か不明の断面は`excluded_points`へ分離します。レポートはGit対象外のprivate配下に保存されます。

### 保存済みPaper台帳の比較

`uv run python -m hr_platform.cli --root private/real compare-paper --at 2026-09-28T15:00:00Z`

実験設定の三モデルを、記録された全競走で比較します。見送りやデータ不足も分母に残し、
モデルの記録・入力が揃わないと比較を止めます。未精算があれば総損益・ROIはnullです。
精算訂正は照会時点までに保存・公開された最新の1件を使い、同時刻の複数版は未確定として扱います。
これは提供元の改訂順を証明するものではありません。購入額ゼロのROIは未定義です。
利用可能時刻のない旧精算記録は集計せず、同じ精算入力を再投入した現在時刻から利用可能にします。
開催した全競走を網羅したことや、実データで利益が出ることは、この集計だけでは証明しません。

静的診断の`reference_consistency`は、参照市場を同時に表すために必要な最小の確率誤差を記録します。
正則化や対象市場の価格差とは独立した診断です。値が大きいことを依存構造の優位性や利益とは解釈せず、
許容差・購入閾値も自動変更しません。
