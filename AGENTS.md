# hr-platform
- 正本は docs/project_brief.md (統合 v0.4)。状態は docs/status.md。
- PRのタイトルと説明は日本語で記載する。通常のPRは独立レビュー・CI・公開境界を確認し、自律的にマージする。人の判断が必要な場合だけ具体的な判断事項と理由を示す。
- 調査・設計・実装はメイン単独。サブエージェントは編集しない独立批判レビューのみ。
- 地方競馬の平地、Paperのみ。Windows、購入データ、実投票機能・認証情報は禁止。
- 原本・試行・観測・解析版を保持。同値の再観測と再配送を区別。available_atでas-ofを制限。
- 実データ、原本、台帳、詳細出力、SecretsをGit/CI/PRへ出さない。合成fixtureは tests/fixtures/synthetic。
- quant-platformへ移動・統合しない。Cloudflare変更は指定された環境・承認内のみ。
- `make test` と `make demo`。実測とmock、未実行を区別して docs/status.md を更新する。
