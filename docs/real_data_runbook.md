# 実データを取り込む手順

## 今できること

有限サンプル取得、取得済みZIPの検査、原本保存、収集Workerのmanifest付き原本の取込み、履歴/as-of/再解析をコマンドから実行する。
NARへのローカル通信は共通の有限取得処理へ集約する。`collect-sample`は一枠、`paper-session`はその処理と固定判断・精算をつなぐ。生のCSVだけではレース状態は`UNKNOWN`、Paper対象外。状態証跡と事前登録を使う探索Paperは後半の手順に従う。

公式説明書2026-09-16版に記載されたオッズ10列、レース一覧66列、出馬表36列、払戻54列を実装した。
合成試験に加え、Mac取得の実CSVでUTF-8 BOMのヘッダー・基本表記を確認済み。全例外・全券種への適合を意味しない。
実ファイルで差が見つかった場合は、原本を残して解析版を変更する。

## 最初に試す：ZIP一組から戦略を比較する

個人研究の初回実験は、この静的診断を優先する。監視・バックアップ等の追加整備は不要。取得済みのオッズZIPとレースZIPを使う。

```sh
PYTHONPATH=src uv run python -m hr_platform.cli diagnose --zip private/inbox/取得済み_odds.zip --race-zip private/inbox/取得済み_race.zip --kind DAILY_SNAPSHOT --encoding utf-8-sig
```

文字コードは実ファイルで確認して指定する。`--race '<日付:競馬場:競走番号>'`は任意。未指定なら、レースID順で平地・頭数一致・必要な買い目の完全性を満たす最初の一競走を選び、値差や結果で選ばない。データ検査に失敗した場合はパーサの適合を確認し、戦略の失敗と混同しない。

- 原本と検査結果を保存し、馬単の直接換算・同一周辺の比較分布・対象の馬連を校正から除いた主モデルを同じ競走・断面で比較する。
- 出力は既存のモデル診断一式。価格差を依存寄与と残りへ分け、正則化・参照重みへの感度、部分識別を調べる。市場インプライド確率を現実の勝率とは表示しない。
- 結果欄・公式払戻を推定入力へ渡さない。ばんえい・未知の馬場区分・不完全な入力は除外し、取消馬を勝手に削って計算を通さない。
- 詳細は非公開レポート。取得時刻・as-ofはnull、Paper対象外。この診断コマンドはFINAL_ONLYを受け付けない。月次の内容検査は`inspect --kind FINAL_ONLY`を使う。当日ファイルも中間オッズとは限らないため、取得時刻不明や結果欄ありの診断をベット時点の実験とは扱わない。
- 初回はファイル互換性と構造計算ができるかの確認であり、戦略がワークするかの検証ではない。実際に判断時点までに取得・解析できた中間オッズと推移が揃ったら、正則化や丸めで差が消えるかを調べ、同条件の複数競走・公式精算へ進む。一断面から収益性の成功を主張しない。

## 有限サンプル取得

承認済みの低頻度・少数のローカル実験に使う。常時稼働の予約機能は持たない。各回は同じ`--root`を使い、過去の拒否・待機・未完了試行を共有する。rootや経路を変えて停止を回避しない。既存Cloudflare経路の停止はこのコマンドで解除しない。

取得前にprivate配下へ以下の計画を保存する。時刻は実際の将来時刻、scopeは取得日の`YYYYMMDD`にする。最大6項目、全体24時間以内、各枠120秒以内、要求の間隔120秒以上。最初の枠より前に一度呼ぶと計画を登録して`WAIT`を返す。

```json
{
  "format": "nar-finite-local-v1",
  "requests": [{
    "id": "odds-1", "kind": "odds", "scope": "20000101",
    "url": "https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=daily",
    "at": "2000-01-01T10:00:00+09:00", "until": "2000-01-01T10:01:30+09:00"
  }]
}
```

```sh
PYTHONPATH=src uv run python -m hr_platform.cli --root private/real collect-sample \
  --plan private/sample-plan.json --item odds-1
```

- 枠内で同じコマンドを呼ぶと1回だけ取得する。枠外は取得せず、終了後の再実行は保存済みreceiptから再読出し・解析する。過去の枠を回収したとは扱わない。
- `odds`は当日オッズZIP、`race`は同じ公式導線のRaceDataDownload。`state`は事前選択した単複ページ、`payout`は同じ競走の成績ページ。HTMLのscopeは`日付:場:競走`で、URLの競走識別と照合する。任意ホストは受け付けない。
- 月次オッズの内容検査は`kind=monthly_odds`、`scope=YYYYMM`と、公式画面で実際に確認した月次URLを指定する。URLの年・月とファイル名を照合し、`FINAL_ONLY`の資産検査だけを行う。原本・取得receipt・検査結果を対応づけ、オッズ観測・Paperへは登録しない。`paper-session`への指定も拒否する。
- 月次は全種共通の停止・120秒間隔に加え、同じrootの前回月次要求の窓終端から24時間以上空ける。失敗も対象で、同じイベントの再読出しは追加通信しない。外部ブラウザ等の過去試行はこの索引に入らないため、確認済みの前回試行も踏まえて計画する。
- リダイレクト・自動再試行は行わない。403/404/CAPTCHA等は停止、429はRetry-After以上待機。読込失敗・未完了試行でも次の通信を停止する。解析だけの失敗は原本を隔離し、次の計画枠の取得を妨げない。
- 原本、限定した応答ヘッダー、各時刻、バイト数、所要時間、失敗・再配送をprivateへ保存。Cookie・認証ヘッダーは保存しない。失敗したオッズ取得は共通の試行履歴にも残す。
- 同値の別取得は別観測、本体だけハッシュで共有する。解析を後で復旧した場合も、その時点より前のas-ofを変えない。実データの公開CI実行は拒否する。

## 1. 取得時刻が分からないZIPの検査

リポジトリのルートから実行する。ファイル名は手元の実際の名前へ置き換える。文字コードは実ファイルで確認して指定する。

```sh
PYTHONPATH=src uv run python -m hr_platform.cli inspect \
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
PYTHONPATH=src uv run python -m hr_platform.cli import-capture \
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
- 成功した200/304の原本取込みとは別に、D1の失敗・待機・保存途中の記録を下記の取得ログとして取り込める。実際に有効だった取得計画とCloudflare内の市場索引への接続は次段階。CLIの履歴に記録がない区間は、取得成功や完全な計画被覆を意味しない。

## 3. 履歴、時点再現、再解析

```sh
PYTHONPATH=src uv run python -m hr_platform.cli history --race '<日付:競馬場:競走番号>' --market quinella
PYTHONPATH=src uv run python -m hr_platform.cli asof --race '<日付:競馬場:競走番号>' \
  --market win --market exacta --market quinella --at '<タイムゾーン付き日時>'
PYTHONPATH=src uv run python -m hr_platform.cli reparse --observation '<event_id>' \
  --version repair-v2 --encoding cp932
PYTHONPATH=src uv run python -m hr_platform.cli metrics
```

どの照会も詳細結果は非公開ファイルへ保存する。as-ofは `available_at` までに使えた観測だけを返し、元の取得時刻・鮮度・既知の欠測を残す。
後日取り込んだ原本や再解析結果を、過去へ戻して利用可能だったことにしない。`reparse`で同じ版を再配送しても時刻は更新しない。
解析結果が誤っていれば新しい版で修正する。既存の判断は変更しない。

## 4. レース状態・払戻の扱い

レース一覧の発走時刻をJSTとして読み、出馬表の結果欄とレース一覧のラップ・通過順等は別に保持する。
結果が空でも発売中とは推測しない。馬連・三連複などの払戻買い目を読み、同着による複数行の共通買い目は重複排除する。
同じ買い目の金額が矛盾した場合や、返還・特払等の未知表記は隔離する。

現行CSVだけでは発売状態・取消/除外・最終確定・返還の完全性を確認できない。CSVの未知状態を維持し、後半の公式HTML証跡を使う。成績HTMLで確認できた通常の馬連・三連複払戻と除外返還は既存Paper判断の精算に接続できる。実測の件数・制約は[状態報告](status.md)に集約する。

## 5. Cloudflare開発環境

| 項目 | 設定 |
|---|---|
| Worker | hr-platform-dev-ingestion |
| D1 | hr-platform-dev-index |
| R2 | hr-platform-dev-private |
| 収集 / 内部の取得条件確認 | false / false |
| Cron / HTTP公開 / preview | なし / 無効 / 無効 |

既存の接続アカウントで専用リソースを作成した。既存サービス・公開設定・契約は変更しない。
D1 migrationは新設DBだけへ適用し、Workerは上記の停止設定で配置する。
Cloudflareの有限取得は2回の404で停止済み。過去の[有限取得計画](cloud_sample_plan.md)を再開の指示として使わない。`SOURCE_APPROVED`は内部の条件確認スイッチであり、個別の許可申請が必要と確認した意味ではない。通常ダウンロードの申請要否と、今回の自動取得条件を [source_capabilities.md](source_capabilities.md) で区別する。

```sh
# 合成データだけで、開発用D1/R2の保存・2時点の履歴読戻しを確認する。
# MacからCLIで操作する疎通試験。定常運転の方式ではない。
uv run python scripts/cloud_storage_probe.py --execute
```

probeは `development-probes/` と `development_probes` テーブルに少量の合成データだけを置く。取得/観測/Paper台帳へは混ぜない。
ログ・結果は `private/cloud-probes/` に置く。通常CIでは実行禁止。計測はCLI往復を含む経過時間で、WorkerのCPU時間や請求額ではない。
Workerの `duration_ms` は保存前までの取得段階、D1の `processing_ms` は成功した一回の実行で原本・観測公開が終わるまでの経過時間（最後の計測値保存を除く）。再配送回復はその実行分を記録する。計測保存が失敗しても観測を失敗へ戻さない。

## 6. 少数サンプルと継続収集の残条件

1. 自動取得・クラウド保存・robotsの適用関係を公開資料等で確認し、条件と根拠を記録する。[問い合わせ文](source_capabilities.md)は確認方法の選択肢であり、申請義務やPRマージの必須条件としない。外部への送信は最新指示により人が行う。[確認先と文面](human_actions.md)を用意してあり、エージェントは送信しない。
2. 承認済みのMac有限サンプルを共通CLIで取得し、原本・receipt・観測索引を照合する。Cloudflareは停止を維持する。文字コード、全券種の行数/意味、ファイル時刻、出走状態・返還の未適合例を実ファイルで検査する。取得済みZIPのオフライン解析も継続する。
3. 条件が許す収集頻度・開催時間帯を設定してM2の継続保存を開始する。モデル失敗と収集を切り離す。
4. 取得ログへ実際に有効だった取得計画を接続し、未実行枠と被覆率を確定する。クラウド内正規化、公式状態/払戻、実時間Paperを接続する。Mac常時稼働を定常構成にしない。

実データCLIは公開CIで停止し、保存先はGitでignoreされた `private/` の下位ディレクトリだけに制限する。
root内部のsymlink・共有ファイルへの書込みも拒否する。原本・レポート・台帳をGitへ追加しない。

## 7. 成功・失敗・待機の取得ログ

管理用exportは専用dev D1に対するSELECTを1回だけ実行する。NARへ通信せず、デプロイ・収集設定も変更しない。指定期間は開始を含み終了を含まない最大24時間、最大10,000件。上限超過や不完全な応答は成功扱いしない。

```sh
uv run python scripts/export_collector_log.py --from '2026-09-28T00:00:00+09:00' --until '2026-09-28T22:00:00+09:00'
# 表示された private_export のパスを使う。
PYTHONPATH=src uv run python -m hr_platform.cli import-log --file '<private_exportのパス>'
PYTHONPATH=src uv run python -m hr_platform.cli capture-log --from '2026-09-28T00:00:00+09:00' --until '2026-09-28T22:00:00+09:00' --at '<照会時点のタイムゾーン付き日時>'
```

- export、Wranglerのstdout/stderr、実行時間・D1使用量メタデータはGit対象外へ保存する。標準出力は状態と非公開ファイルのパスだけ。公開CIではexportと実CLIを停止する。
- このJSONは管理者が自分のD1から取得したものを信頼する境界にある。電子署名や第三者への取得証明ではない。
- 取得失敗の受信時刻、未送信の待機記録の取得開始時刻はnullを維持する。予定時刻や取込み時刻で代用しない。
- exportごとの状態を保持し、同じexportの再配送では件数と利用可能時刻を変更しない。保存失敗から成功へ復帰しても、以前の照会時点へ成功を混ぜない。読出し期間が重なって順序を確定できない場合は、ORDER_UNCERTAINとして両候補を保持する。
- D1の読出し時刻とローカル利用可能時刻を分ける。前者だけを根拠にローカルの過去へ情報を戻さない。取込み前の照会では取得ログも見えない。
- ログだけではオッズ観測・Paperを作らない。`local_parse_available` は同じイベント・原本に対応した既存のローカル解析があるかを示し、特定競走・券種の適格性を保証しない。オッズ値の時点照会には従来の`asof`を使う。
- `audit_slots`は開始時刻から120秒ごとの監査用区切り。`NO_CAPTURE_RECORD`はその区間内の予定時刻に対応する記録が見つからないという意味で、実際にCronが有効だった証拠ではない。実取得計画未接続のため`scheduled_coverage=null`。停止中の期間を0%稼働や欠測なしと断定しない。
- 今回の実Cloudflare検証は停止中D1の空ログの読出し・取込み・再読出し。成功/失敗を含む非空ログ、修復、再配送は合成試験で検証する。

## 判断時点と最終オッズ

判断入力は、判断時刻以前に利用可能になった中間オッズと、その時点までの推移だけ。初期はその時点で公表済みの発走予定時刻の10分前を維持する。推移の窓も同じ時刻で切り、後続の観測・最終オッズ・補間値を入れない。

「最終直前」は事後に最終オッズと比較して都合のよい断面を選ぶ基準にしない。より直前の実験を追加する場合も、判断時点・許容遅延・鮮度を事前に別版として固定する。締切までに使えなかった値では仮想購入しない。公式払戻・返還は事後精算でのみ使い、最終オッズをベット時の倍率として固定しない。

### 特払いの正規化入力

精算関数は `special_payouts` に `market` と `payout_per_100`（整数70または80）の一覧を受け取る。
これは式別全体への特払いを公式情報から確認して正規化した入力であり、オッズ0.0や的中買い目の欠落から自動生成しない。
実ファイルからの特払い自動抽出は未適合。未確認の表示を通常の不的中へ変換せず、精算入力の完全性を保留する。

- `final` と `complete_markets` が揃うまでは未精算。
- 対象式別の購入券へ適用し、全額返還対象の券は返還だけを計上する。通常的中払戻との混在、全競走無効との混在、重複式別、不明額は拒否する。
- `special_payout_yen` は `payout_yen` の内数。損益やROIへ二重加算しない。
- 元入力と版を保存し、訂正も既存の精算履歴・利用可能時刻の規則に従う。

### 保存した公式成績ページとCSVの払戻照合

```sh
PYTHONPATH=src uv run python -m hr_platform.cli --root private/real check-payout \
  --race-zip private/inbox/race.zip --html private/inbox/result.html \
  --race 'YYYYMMDD:競馬場:競走番号' --encoding utf-8-sig
```

通信せず、成績ページ見出しの競走識別と、払戻表の買い目・金額をレースCSVに照合する。
原本と詳細はprivate内へ保存し、標準出力は状態・レポートのパスだけ。
`MATCHED_UNQUALIFIED` は掲載内容の一致であり、取得元の真正性、返還の網羅性、結果の最終性の証明ではない。
入力は手元の取得記録と対応する保存ファイルを使う。オッズ観測・Paper判断・精算は生成しない。
不一致は `MISMATCH`、不明な表現や別競走は `QUARANTINED`。
特払い・返還などの例外HTMLは未適合であり、数値へ推測変換しない。

## 参照市場が整合しない場合の研究方針

`configs/research.yaml` は従来の設定を保持し、Qsetが空なら `REFERENCE_INCONSISTENT` で見送る。
数値計算に失敗した場合の `MODEL_ERROR` とは区別する。既存台帳を再分類・書換えしない。

新しい探索版 `configs/research-shadow.yaml`（`IRCD-v0.4-exploratory-002`）は、
`reference_constraint_policy: allow_inconsistent_shadow` を明示する。
Qref/Qmargの数値検証が通った場合に限り、Qsetが空でも仮定付きの研究用判断へ進める。
各判断に `INCONSISTENT_REFERENCES_SOFT_CALIBRATION` を残し、比較表でも不整合と仮定の件数を表示する。
この方針は `EXPLORATORY_SHADOW` 専用で、`FROZEN_PAPER` へそのまま流用すると拒否する。

- 元のε、`INCONSISTENT`、nullの識別上下限、参照残差・最小必要誤差・感度診断を保持する。
- λ・重み・判断時刻・金額・閾値は従来設定と同じ。競走ごとにεを広げない。
- 三モデルは同じ入力・競走集合で比較する。数値失敗、欠測、鮮度、競走状態、締切の条件は維持する。
- この変更は保存済みサンプルの不整合を見て設計した探索方針である。過去サンプルを事前登録済み評価や過去のPaper判断へ変更しない。新しい実時間判断だけを新実験版へ記録する。
- 静的診断・過去as-of診断は、この設定でもPaperを生成しない。不整合のstatusは `REFERENCE_INCONSISTENT`、CLI終了コードは非0のまま。

```sh
# 保存したZIPによる構造の診断。購入・過去判断の生成はしない。
PYTHONPATH=src uv run python -m hr_platform.cli --root private/real diagnose \
  --zip private/inbox/odds.zip --race-zip private/inbox/race.zip \
  --kind DAILY_SNAPSHOT --encoding utf-8-sig --config configs/research-shadow.yaml
```

## 公式単複ページの状態表示を保存する

`import-state`は保存済みの公式単勝・複勝オッズHTMLと、自分の取得処理で記録したreceiptを取り込む。NARへの新たな通信はしない。現在の対応は「最終」「HH:MM現在」の見出しと「競走除外」の表示。空の変更欄は`NO_CHANGE_DISPLAYED`で、出走有効とは断定しない。未適合の見出し・変更表現は未知として残す。

```sh
PYTHONPATH=src uv run python -m hr_platform.cli import-state \
  --html private/inbox/odds-page.html --receipt private/inbox/odds-page-receipt.json \
  --race '<日付:競馬場:競走番号>'
PYTHONPATH=src uv run python -m hr_platform.cli state-asof \
  --race '<日付:競馬場:競走番号>' --at '<照会時刻>'
PYTHONPATH=src uv run python -m hr_platform.cli state-history \
  --race '<日付:競馬場:競走番号>' --at '<照会時刻>'
```

receiptには`url`、`status`（200）、`sha256`、`bytes`、`fetch_started_at`、`headers_received_at`、`collector_received_at`、`raw_saved_at`を使う。時刻はタイムゾーン付きの実測値。公式OddsTanFukuのURL、原本ハッシュ・サイズ、時計の順序を検査し、HTMLの選択中の競走リンクと見出しで日付・競走・競馬場を照合する。receiptは自分の取得記録を信頼するもので、第三者提供ファイルの真正性を保証するものではない。

- 原本・receipt・状態観測・解析版を別に保持する。同じ取得の再配送は初回時刻を維持し、同じ原本の別時刻取得は別観測になる。
- 利用可能時刻はローカルで原本・解析結果を保存した後の実時計。`state-asof`はその時点までの解析だけを返す。`state-history`で全観測・解析版を読戻せる。後の最終表示・除外表示・再解析を過去へ戻さない。
- `FINAL_DISPLAYED`は表示オッズの最終扱いであり、競走終了・公式払戻の最終性を保証しない。
- `HH:MM現在`は`CLOCK_DISPLAYED`として、`displayed_time_of_day`に時刻だけを保持する。競走日・取得日から更新日を補わず、`source_updated_at`はnull、理由は`DISPLAY_DATE_UNQUALIFIED`。時刻表示だけでは発売中・中間オッズの適格性を確定しない。
- 選択中の単複タブはOddsTanFukuのパスと競走情報で照合する。同じCSSクラスを持つライブ中継リンクは競走識別に使わない。旧版で隔離された原本は、v4で同じ`import-state`を実行すると新しい解析版となり、旧as-ofは維持される。
- 状態欄・見出し等の明示的な非表示要素は隔離する。枠列にある公式HTMLのrowspanと非表示placeholderは馬番をずらさず扱う。
- この段階では状態資料をオッズの既存観測へ結合せず、Paper判断・精算を生成しない。将来の中間表示・有効出走集合の適合と、判断時点の状態との照合が次の接続条件となる。

## 公式払戻の取込みと既存Paperの精算

保存した公式 `RaceMarkTable` の成績表・払戻表と取得記録を使う。記録形式は状態表示と同じで、URL・本文ハッシュ・サイズ・取得開始・ヘッダー受信・本文受信・原本保存の各時計を検証する。

```sh
PYTHONPATH=src uv run python -m hr_platform.cli --root private/research import-payout \
  --html private/input/result.html --receipt private/input/result-receipt.json --race RACE_ID
PYTHONPATH=src uv run python -m hr_platform.cli --root private/research payout-history \
  --race RACE_ID --at AS_OF_TIME
PYTHONPATH=src uv run python -m hr_platform.cli --root private/research payout-asof \
  --race RACE_ID --at AS_OF_TIME
PYTHONPATH=src uv run python -m hr_platform.cli --root private/research settle-payout \
  --decision EXISTING_DECISION_ID --evidence IMPORTED_EVIDENCE_ID
```

- `import-payout` は原本・取得記録・観測・解析版を専用テーブルへ保存。利用可能時刻は保存後の実時計。同じ取得の再配送は冪等で、同値でも別時刻の取得は別観測になる。成績情報は判断入力の市場断面・事前状態へ追加しない。
- 自動適合範囲は、数値着順に同着がなく、通常の掲載払戻が整合する馬連・三連複。払戻額は100円当たりの掲載額を使う。除外馬を含む馬番号の組合せは100円返還とする。公式の根拠は [source_capabilities.md](source_capabilities.md)。
- 取消は発売前なので返還へ変換せず、その馬を購入した判断は精算エラーにする。中止は出走済みで返還なし。枠式・重勝式、同着、特払い、不成立、未知の表現・欠けた行はこのアダプターで適合扱いにしない。既存精算器の正規化済み例外処理と合成テストは維持する。
- `settle-payout` は保存済みの判断だけを精算する。判断を新規生成しない。別競走・利用可能時刻の逆転・未適合証拠を拒否し、同じ解析版の再精算は同じ記録を返す。訂正は新しい原本・解析版として残す。
- 標準出力は状態と非公開レポートのパスのみ。原本・台帳・レポートはGit対象外。実データCLIは公開CIで使用しない。

実物の1競走で除外表示・通常払戻の取込み、再配送、as-of、再読出しを確認した。実時間Paper判断はまだなく、実際の購入判断に対する精算は未実施。合成判断で払戻・返還・外れ・取消時停止を試験した。

## Python Workerのモデル実行試験

[Python Workers](https://developers.cloudflare.com/workers/languages/python/)の
[対応パッケージ](https://developers.cloudflare.com/workers/languages/python/packages/)を使い、
既存の `model.py` をそのまま実行する小さなRPC入口を追加した。モデルだけの実験であり、
取得・as-of適格判定・Paper台帳・精算を接続した実運転ではない。

```sh
PYTHONPATH=src uv run python scripts/prepare_python_worker.py --out private/python-worker-build
cd private/python-worker-build
uvx --with 'uv==0.12.3' --from 'workers-py==1.17.4' pywrangler dev \
  --ip 127.0.0.1 --port 8791 --no-show-interactive-dev-session
```

準備先は新しいGit対象外ディレクトリを指定する。ビルド対象の `src/` にはモデルと入口の
明示したPythonファイルだけをコピーする。仮想環境や実データは置かない。
依存内のテストデータ・型定義・pycは `python_modules.exclude` で配布から外す。
数学実装を別言語へ複製せず、依存版は `workers/research/pylock.toml` に固定する。
科学計算パッケージはRPC呼出し内で読み込む。起動時のimportではSciPyの乱数初期化が失敗した。

呼出しはサービスバインディング `MODEL` から
`await env.MODEL.analyze(JSON.stringify({runners, markets, config}))`。
返値はJSON文字列で、入力は既存 `analyze` と同じ形式。
HTTP入口は常に404、生成設定は公開URL・preview・Cronなし、ストレージ等のbindingなし。
この手順はローカル起動だけで、デプロイを行わない。

RPCは1MiB、3〜16頭、最大300反復などを検査し、モデルの価格検証も共用する。
`ANALYZED` / `REFERENCE_INCONSISTENT` は計算の状態で、判断許可ではない。
常に `MODEL_ONLY_NOT_PAPER_DECISION`、`paper_decision_created=false`、
`live_execution_qualified=false` とし、呼出し時間と実際の依存版を返す。
実データを使う場合の入力・返値は非公開保存し、公開CIへ渡さない。
v2では進まない内部時計のduration_msをnullにする。経過時間はCPU時間ではなく、
課金CPU時間はCloudflareの計測値を別に確認する。

2026-09-29にローカルworkerd、30日にCloudflare上の実RPCで合成入力と保存済み実断面を計算した。
依存版を揃えたnative比較でも実断面1件は確率差の基準外で、実行環境による差が残る。
クラウドで合成入力・実8断面の計算とCPU時間を確認した。追加の頭数別合成試験では
4・9・16頭が成功し、12頭の1例は`MODEL_ERROR`を返した。
同じ12頭の入力をローカルWASMで再現すると、感度計算の収束精度不足で停止した。
HTTP200や基盤側のsuccessだけでモデル成功と数えず、返値のstatusも確認する。
全頭数・券種・入力条件・連続/同時実行の稼働保証はない。
原本解析と履歴の入口は下記。収集からの自動起動・状態判定・Paperへの接続は残る。
詳しい検証範囲は [status.md](status.md)。


## Cloudflare内の原本解析と履歴

`prepare_python_worker.py --with-storage --out private/新規ビルド先` は、既存devの
R2 `RAW` とD1 `INDEX`を設定へ加える。クラウドの変更前には対象を確認し、
`migrations/0003_odds_history.sql`を適用する。この準備コマンド自体は配置・取得を行わない。
公開URL・preview・Cronなしを維持する。HTTPは404で、サービスバインディングからだけ呼ぶ。

```js
const result = JSON.parse(await env.RESEARCH.odds(JSON.stringify({
  operation: "normalize", observation_id: existingObservationId
})));
```

- `normalize`：既存の`raw_observations`とR2原本を読む。取得処理は呼ばない。
  観測IDは収集Workerの`nar-daily-odds:<予定時刻>`、または取得証跡付き取込みの
  `nar-mac-import:<receiptのSHA256>`。Mac由来は`IMPORTED_MAC_RECEIPT`として区別する。
- `history`：`race_id, at`と任意の`limit`（1〜50）、`cursor`を渡す。
  内容が約1MiBに達した場合もページを分ける。`next_cursor`がある間、同じ`at`で続ける。
  原本・観測・解析版・利用可能時刻を返す。
- `asof`：`race_id, markets, at`と任意の`max_age`（秒）を渡す。
  指定時点で使えた最新の解析版を選び、元の受信時刻と鮮度を残す。
  後の解析で削除された競走・市場を旧版から復活させない。

CSVの行解析はローカルと共通。v2は競走別の本体と小さなmanifestをR2へ保存する。
全競走の保存・索引化が終わるまで利用可能にせず、D1の公開UPDATE文の実行時刻を
`available_at`にする。旧v1の履歴も読み出せる。同値の別観測は残し、同一イベントの再送は増やさない。
競走ブロックが離れて再登場するCSVはv2のクラウド解析では未対応として拒否する。
後続行の異常や保存中断で未公開になった本体を判断入力へ流さない。原本は残る。

出走・発売状態との結合、取得計画の欠測判定、モデル・Paperの自動起動はまだない。
`gap_coverage=UNQUALIFIED_CAPTURE_PLAN`、`gaps=null`、`paper_eligible=false`を返す。
`FINAL_ONLY`は履歴へ残すがas-ofの判断入力へ出さない。解析RPCの成功だけではPaper適格性を意味しない。
返値、実験計画、取得証跡は非公開保存する。合成試験とMac原本の取込みは収集Workerの取得成功数に数えない。

実ZIP1個のクラウド解析ではwall約66秒・CPU約2.2秒を実測した。
呼出し元のタイムアウトは処理中止を意味しないため、同じ観測ID/解析版のD1状態と履歴を先に確認する。
今回も60秒で呼出し元が待機を打ち切った後、保存完了と履歴/as-ofの読戻しを確認した。
実測は1原本に限る。全データ量・頭数・同時実行での制限適合を保証するものではない。

## 競走情報の履歴と将来のPaper計画

以下は取得済み原本のローカル処理。ネットワーク取得・Cron設定・デプロイは行わない。
原本、receipt、レポート、計画IDは`private/`配下に保持する。

```sh
uv run python -m hr_platform.cli --root private/real import-metadata \
  --zip private/取得済みrace.zip --receipt private/取得記録.json --date YYYYMMDD
uv run python -m hr_platform.cli --root private/real metadata-history \
  --date YYYYMMDD --at '利用可能時刻の上限'
uv run python -m hr_platform.cli --root private/real paper-plan \
  --race '日付:競馬場:競走番号' --config configs/research-shadow.yaml
uv run python -m hr_platform.cli --root private/real paper-tick --plan '保存された計画ID'
```

`import-metadata`は当日RaceDataDownloadのUTF-8 BOM ZIPと、URL/status/sha256/bytes/filename/
fetch_started_at/headers_received_at/collector_received_at/raw_saved_atを持つreceiptを検証する。
一つのZIPを一観測として保存し、競走別読出しで内部配分する。月次・取得時刻不明のZIPは対象外。
同値の再観測は残り、同じイベント/解析版の再配送は増えない。`metadata-asof`も利用できる。
同じreceiptと原本の再解析は`MetadataEvidence.ingest(..., version=新しい版)`を使い、
利用可能時刻は再解析時点として記録する。全解析版の履歴は保持する。

`paper-plan`は利用可能な競走情報から予定を決め、判断時刻前にだけ新規登録する。
返値の`private_report`を開くと計画IDが分かる。同一実験の設定変更や過去時刻への新規登録はエラー。
登録完了が基準時刻を越えた計画は見送りにする。未判断の`paper-plan`/`paper-tick`は、
現在までに判明した予定変更を履歴付きで反映する。変更後の基準時刻が過去なら遡って購入しない。
`paper-tick`は早ければ`NOT_DUE`で台帳を作らず、判断後の再実行は既存の三モデル判断を返す。
遅延・欠測・古い証跡・最終表示などは見送りとして固定する。後で届いた原本で見送りを取り消さない。

実データでは判断までに、同じ保存先へ対象/参照オッズ、`import-state`による単複表示、
新鮮な`import-metadata`を取得証跡付きで取り込む必要がある。予定登録だけでは入力はそろわない。
方針・仮定は[実験計画](experiment_plan.md#取得証跡を使う将来のpaper入力)を参照。
この処理のCloudflare配置・自動収集接続はまだ行っていない。

## 一競走の有限Paper実験を進める

`paper-session`は、既存のPaper計画と有限取得計画を組み合わせる。取得計画は同日のオッズ2枠以上、競走ZIP、同じ競走の状態・成績ページを含む最大6枠。事前に保存し、最初の取得枠より前に一度呼んで登録する。Paper計画や対象競走を、得られた値を見て選び直さない。

```sh
PYTHONPATH=src uv run python -m hr_platform.cli --root private/real paper-session \
  --paper-plan '<登録済みPaper計画ID>' --sample-plan private/sample-plan.json
```

- 一度呼ぶと、予定枠内の取得、固定時点の判断、保存済み公式払戻による既存判断の精算を順に進める。まだ先の処理はprivateレポートの`next_at`へ返す。
- 判断前のオッズ・競走情報・状態は、設定の鮮度以内に取得・解析する。遅れて利用可能になった値は固定as-ofへ入らない。取得時刻を判断基準へ動かしたり、最終オッズを代わりに使わない。
- `--wait-seconds 3600`なら、最大1時間の範囲で取得から判断・払戻まで同じ有限プロセスで進められる。既定0。次の処理がその範囲外なら即時に戻る。対話側はプロセスを起動してハンドルを保持し、15分ごとの再開を前提にしない。取得枠・共通間隔・期限超過の扱いは変えない。
- モデルの想定外例外も`MODEL_ERROR`の見送りとして固定する。段階の障害はクラス名だけ記録し、後続の取得を継続する。提供元の拒否による収集停止は維持する。
- 公式払戻が未掲載・不適合なら未精算のまま。予定外の再取得や0円精算をしない。同じコマンドの再実行・再起動でも、通信・判断・精算を増やさない。
- `WAITING`は次の処理時刻あり、`INCOMPLETE`は追加の予定がなく未完了、`COMPLETE`は三モデルの判断と対応する公式精算の完了を示す。見送りも母数に残す。継続収集・FROZEN_PAPER・収益性の成功とは別に報告する。

実時計では未来のレース前後に呼ぶ必要がある。保存済みの過去データを渡すだけで、当時実行できたPaperを作るコマンドではない。

## 9. 過去履歴の定期蓄積

`wrangler.archive.jsonc` の `hr-platform-dev-archive` は、第三者サイトの公開画面が使うJSONを読む専用Worker。
既存の非公開R2/D1を共用し、モデル・Mac・対話の起動に依存しない。公開HTTP入口は404のまま。

- 初期範囲は過去7日。JSTで前日までを日付・ページ単位で順に保存し、毎日新しい前日分を追加する。完了ページを再取得しない。
- Cronは5分ごとに確認し、取得処理の終了から5分以上空けて次の1ページを取得する。実際の取得間隔は5〜10分程度になる。並列取得やブラウザ描画は使わない。
- 過去日の一覧にも`time=00:00`を指定する。指定なしでは先頭が時刻で移動するため、v1で完了したページは移行0005で一度だけ再取得待ちへ戻す。旧原本・試行は残り、v2 receiptのURLで新しい取得条件を確認できる。
- 1ページに複数競走・券種が入る。画面の最大表示と同じ24時点を要求し、提供された本体を一度だけ保存する。人気順の組合せ欠落や、時点数の不足は補完しない。ページの保存完了を日全体の完全性の証明にせず、解析時に競走IDの重複・欠落と配布側の総件数の変化も確認する。
- R2の `archive/raw/<sha256>` に原本、`archive/receipts/` に取得証跡、`archive/manifests/` に競走・券種・時刻ラベルの索引を保存。D1の `archive_attempts` と `archive_jobs` で履歴・再配送・進行状況を読む。
- `available_at` は今回クラウドで保存・公開した時刻。過去の時刻ラベルを当時の利用可能時刻へ変換しない。`source_updated_at` と `historical_available_at` はnull、`paper_eligible=false`。最終ラベルは `FINAL_ONLY`、時刻だけのラベルは `CLOCK_ONLY`。
- 平地と確認できない競走は索引で除外表示する。複数競走を含む配布原本はそのまま残すが、ばんえい等を研究対象に含めない。
- 401/403/404・リダイレクト・CAPTCHA・想定外形式で停止。429は提供元の待機指定以上待つ。同じイベントを再配送しても取得数は増えない。保存・解析失敗は原本を残し、再解析の準備として使う。
- NAR公式の停止状態、当日取得枠、Paper台帳は変更しない。初期調査用のprivate HTTPスクリプトは定期処理に使わない。

`wrangler.archive.jsonc` は承認済みdevの現在の設定（有効・5分ごとのCron）を保持する。
通常の `npm run build` はdry-runのみ。明示的な配置はこの設定を使い、停止するときは同WorkerのCronを空にするか、`ARCHIVE_ENABLED` をfalseへ戻して配置する。新規の契約・ログイン情報は不要。
実行済みの版・件数・制約は[状態報告](status.md)を参照する。
