# 実データを取り込む手順

## 今できること

取得済みZIPの検査、原本保存、収集Workerのmanifest付き原本の取込み、履歴/as-of/再解析をコマンドから実行する。
NARへ通信するコマンドは含めない。実データへの適合確認が済むまで、すべて `live_qualified=false`、レース状態は `UNKNOWN`、Paper対象外。

公式説明書2026-09-16版に記載されたオッズ10列、レース一覧66列、出馬表36列、払戻54列を実装した。
仕様書の項目名に適合する合成データで試験しており、実CSVのヘッダー・表記・文字コードへ適合したという意味ではない。
実ファイルで差が見つかった場合は、原本を残して解析版を変更する。

## 1. 取得時刻が分からないZIPの検査

リポジトリのルートから実行する。ファイル名は手元の実際の名前へ置き換える。文字コードは実ファイルで確認して指定する。

```sh
uv run python -m hr_platform.cli inspect \
  --zip private/inbox/取得済み_odds.zip \
  --race-zip private/inbox/取得済み_race.zip \
  --kind DAILY_SNAPSHOT --encoding cp932
```

- `--race-zip`は任意。両方を渡すと、出馬表に存在する馬番集合に対する全買い目の不足・余分を検査する。
- 出馬表に存在することと、その時点で出走が有効なことは別。欠番・取消・除外の実表記が未確認なので、この比較は診断に限る。枠券の完全性は未判定。
- 表示オッズ、レンジ、未知の表記、欠けた券種を区別する。補間や確定オッズによる穴埋めを行わない。
- 月次ZIPは `--kind FINAL_ONLY`。月次形式の名前をDAILY_SNAPSHOTとして渡すと拒否する。
- 原本は `private/real/raw/`、詳細は `private/real/reports/`、索引は `private/real/index.sqlite`。
- 取得記録のないZIPは検査資料として保存する。取得時刻はnullで、観測や過去の判断入力を作らない。
- 解析できない原本も保存して `QUARANTINED` とする。CLIに元データやtracebackを出さない。

## 2. 取得記録付き原本の取込み

Cloudflare収集Workerは、完了した取得ごとにR2の `manifests/<event_id>.json` と、共有原本 `raw/<sha256>` を保存する。
収集開始後、管理者権限で必要なオブジェクトを非公開の作業先へ読み出す。Wranglerは必ず `--remote` と保存先 `--file` を指定し、原本を標準出力へ流さない。

```sh
# <event_id>と<sha256>は私有索引・manifestの実値へ置き換える。
npx wrangler r2 object get 'hr-platform-dev-private/manifests/<event_id>.json' \
  --remote --file private/inbox/capture.json
npx wrangler r2 object get 'hr-platform-dev-private/raw/<sha256>' \
  --remote --file private/inbox/capture.zip
uv run python -m hr_platform.cli import-capture \
  --manifest private/inbox/capture.json --zip private/inbox/capture.zip --encoding cp932
```

この取込みは、私有R2から管理者が取得したmanifestを信頼する境界にある。manifest自体は電子署名ではない。外部から渡された任意のJSONを正当な取得の証明として受け付けない。

- manifestの取得開始・ヘッダー受信・本文受信・原本保存の順序、イベントIDと予定枠、原本サイズとSHA-256を検査する。
- 原本保存未完了のintentは観測にしない。
- 取得時刻とCloudflare上の原本保存時刻を保持し、ローカル取込み・保存・解析・利用可能時刻は実際の現在時刻で記録する。
- ファイル名のUNIX時刻は別項目。市場更新時刻はnullのまま。
- 同じ内容の別時刻の取得は別観測。同じイベントの再配送は初回の取得記録・304の対応先を維持する。
- 304は対応する過去の200を先に取り込む。ETag、原本ハッシュ、順序が一致しない場合は保留する。
- 取り込んだ当日オッズでも、出走状態や発売状態は未知のためPaper判断へ進めない。
- 現版のbridgeは成功した200/304の原本取込み。D1の失敗・停止・未実行枠の一括取込みとCloudflare内の市場索引への接続は次段階。CLIの履歴に記録がない区間は、取得成功や完全な計画被覆を意味しない。

## 3. 履歴、時点再現、再解析

```sh
uv run python -m hr_platform.cli history --race '<日付:競馬場:競走番号>' --market quinella
uv run python -m hr_platform.cli asof --race '<日付:競馬場:競走番号>' \
  --market win --market exacta --market quinella --at '<タイムゾーン付き日時>'
uv run python -m hr_platform.cli reparse --observation '<event_id>' \
  --version repair-v2 --encoding cp932
uv run python -m hr_platform.cli metrics
```

どの照会も詳細結果は非公開ファイルへ保存する。as-ofは `available_at` までに使えた観測だけを返し、元の取得時刻・鮮度・既知の欠測を残す。
後日取り込んだ原本や再解析結果を、過去へ戻して利用可能だったことにしない。`reparse`で同じ版を再配送しても時刻は更新しない。
解析結果が誤っていれば新しい版で修正する。既存の判断は変更しない。

## 4. レース状態・払戻の扱い

レース一覧の発走時刻をJSTとして読み、出馬表の結果欄とレース一覧のラップ・通過順等は別に保持する。
結果が空でも発売中とは推測しない。馬連・三連複などの払戻買い目を読み、同着による複数行の共通買い目は重複排除する。
同じ買い目の金額が矛盾した場合や、返還・特払等の未知表記は隔離する。

現行の公式列だけでは発売状態・取消/除外・最終確定・返還の完全性をまだ確認できない。`final=false`、`complete_markets=[]`、`refund_coverage=UNKNOWN`を維持し、実Paper精算には接続しない。既存の精算器は合成fixtureで検証済み。

## 5. Cloudflare開発環境

| 項目 | 設定 |
|---|---|
| Worker | hr-platform-dev-ingestion |
| D1 | hr-platform-dev-index |
| R2 | hr-platform-dev-private |
| 収集 / 提供元承認 | false / false |
| Cron / HTTP公開 / preview | なし / 無効 / 無効 |

既存の接続アカウントで専用リソースを作成した。既存サービス・公開設定・契約は変更しない。
D1 migrationは新設DBだけへ適用し、Workerは上記の停止設定で配置する。
提供元の条件が解決するまでは収集を有効化しない。

```sh
# 合成データだけで、開発用D1/R2の保存・2時点の履歴読戻しを確認する。
# MacからCLIで操作する疎通試験。定常運転の方式ではない。
uv run python scripts/cloud_storage_probe.py --execute
```

probeは `development-probes/` と `development_probes` テーブルに少量の合成データだけを置く。取得/観測/Paper台帳へは混ぜない。
ログ・結果は `private/cloud-probes/` に置く。通常CIでは実行禁止。計測はCLI往復を含む経過時間で、WorkerのCPU時間や請求額ではない。
Workerの `duration_ms` は保存前までの取得段階、D1の `processing_ms` は成功した一回の実行で原本・観測公開が終わるまでの経過時間（最後の計測値保存を除く）。再配送回復はその実行分を記録する。計測保存が失敗しても観測を失敗へ戻さない。

## 6. 実収集開始の残条件

1. [提供元への確認文](source_capabilities.md)の自動取得・クラウド保存・robots適用関係を解決し、条件と根拠を非公開の運用記録へ保存する。
2. その条件の範囲で複数時点の実ZIPを取得する。文字コード、全券種の行数/意味、ファイル時刻、304、出走状態・返還を実ファイルで検査する。
3. 条件が許す収集頻度・開催時間帯を設定してM2の継続保存を開始する。モデル失敗と収集を切り離す。
4. 失敗・未実行枠を含む履歴索引、クラウド内正規化、公式状態/払戻、実時間Paperを接続する。Mac常時稼働を定常構成にしない。

実データCLIは公開CIで停止し、保存先はGitでignoreされた `private/` の下位ディレクトリだけに制限する。
root内部のsymlink・共有ファイルへの書込みも拒否する。原本・レポート・台帳をGitへ追加しない。
