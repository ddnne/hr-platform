# 保存履歴からのCloudflare研究計算

非公開の独立Workerで、共通の`research_suite`を使う。既存の競馬収集・3方式の実時間Paperと別の処理で、HTTPは404を返す。R2/D1アクセスは`CloudHistory`の共通入口だけを使う。

## 初版の範囲

- NAR平地の保存済み中間オッズを、指定した固定as-ofで再読出しする。解析の`available_at`が後のデータとFINAL_ONLYは入力に使わない。
- 戦略設定・計算コード・依存lock・Worker互換設定を版に対応づける。同じ登録版の変更は拒否する。設定を変える場合は新しい版を登録する。
- 91案を共通数理で計算し、市場欠落や不適格は該当系列のINPUT_EXCLUDEDとして残す。提供元更新時刻が不明な別観測を混合しない。原照会結果も保持する。
- jobは登録版・競走・as-ofで一意。同じjobの再配送で完了済み結果を重複追加しない。中断した計算は同じjobで再試行する。1起動で1jobを処理し、中断は設定したリースと試行数で扱う。
- 結果はR2へ保存後、D1の文実行時刻で公開する。遅れて得た結果を過去へ戻さない。結果・固定入力・処理開始・公開時刻を再読出しできる。

この初版は事後研究の候補計算まで。日額上限の適用、公式精算と最終価格の付与、全期間のcohort自動生成、追加3競技のCloud入力接続は次の作業。結果の`timing_qualified=false`・`settlement_status=NOT_EVALUATED`を、実時間購入や収益性の成功と解釈しない。

## 準備と利用

`scripts/prepare_backtest_worker.py --out private/<新規build>`で、公開ソースだけの新規buildを作る。既定ではCron空・計算無効・公開無効。設定値は`configs/cloud-research.json`、保存用の追加表は`migrations/0011_cloud_research.sql`。

承認済みの非公開環境への配置後、service bindingの`research`へJSONを渡す。

1. `register`: `version`、既存の`base`、8系列の`configs`を登録する。
2. `enqueue`: 返された`bundle_id`、`race_id`、固定時点`at`で保存履歴の計算を予約する。
3. `jobs`: 登録版のjobを一覧する。`after`に最後のjob IDを渡して続きが読める。
4. `result`: `job_id`と照会時点`at`で公開済み結果を読む。

有効化したCronが予約済みjobを処理するため、端末を常時動かす必要はない。実環境での配置・計算・時間・使用量は、実行後に`docs/status.md`へ記録する。
