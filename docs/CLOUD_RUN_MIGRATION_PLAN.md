# CatsDaiko5-Beta Google Cloud Run 移行計画

- 作成日: 2026-11-13
- 対象リポジトリ: `tenma2066-tech/CatsDaiko5-Beta`
- 対象ブランチ: feature/cloud-run-firestore-foundation
- 状態: **隔離ブランチで基盤実装中（GCP構築・実環境移行は未着手）**
- 現行環境: Render（Singapore）
- 移行候補: Google Cloud Run（東京 `asia-northeast1`）
- 目標予算: **月額1,000円以内**
- 最優先: 設定・運用の簡単さ、注文・決済・データの安全性

> この文書は移行計画であり、Cloud Runへのデプロイ、Renderの変更、注文受付の有効化、PayPayログインの再試行を許可するものではない。現行Render環境は、移行完了と明示的な切替判断まで維持する。

## 1. 目的と成功条件

RenderのSingaporeリージョンから、東京リージョンを選べるGoogle Cloud Runへベータ版を複製し、PayPay認証の応答を比較できる環境を用意する。同時に、アクセスや注文がない時間帯のコンピューティング費用を抑える。

成功条件:

1. 公開WebサイトがCloud Run Serviceで動き、最小インスタンス数0でアイドル時にスケール・トゥ・ゼロできる。
2. 注文がない間、注文処理Workerを常時起動しない。
3. 注文ごとの処理が一度だけ実行され、重複決済・重複処理・資格情報の漏えいを防ぐ。
4. 注文状態・進捗・結果がWebコンテナやJobの終了後も確認できる。
5. 既存の管理画面、公開UI、価格計算、PayPay受取確認、暗号化、同時実行制御、キャンセル・手動確認フローを維持する。
6. 既存のGitHub runtime-dataと暗号化キーを保全し、移行前後で照合・復旧できる。
7. 実測した通常運用の月額が1,000円以内に収まる見込みを確認する。無料枠だけを前提にせず、Cloud Tasks、Firestore等のデータ保存、ログ、外向き通信、Artifact Registry、Secret Manager等の料金も見積もる。
8. Render版を壊さず、Cloud Run版を別環境で検証してから切り替える。

## 2. 現行実装の確認結果（2026-11-13）

確認したファイル: `render.yaml`, `app.py`, `github_runtime_store.py`, `shop_store.py`, `requirements.txt`, `docs/BETA_ENVIRONMENT.md`, `docs/BETA_ORIGINAL_UI_PAYPAY_INTEGRATION.md`。

現状:

- FlaskアプリをGunicorn 1 worker / 4 threadsで起動している。
- `app.py` は `multiprocessing` でBCSFE処理プロセスを起動する。
- 実行枠、待機キュー、進捗、結果、転送コード重複防止の一部がプロセスメモリ内にある。
- 待機上限は20件、待機TTLは15分、結果保持TTLは30分、同時実行数の初期値は4。
- 注文・PayPay台帳などのSQLiteデータは `github_runtime_store.py` を通じ、暗号化してGitHubの `runtime-data` ブランチへファイル単位のスナップショットとして保存する。
- GitHub保存のロックはPythonプロセス内のロックであり、別コンテナ・別Job間の分散ロックではない。GitHub Contents APIのSHA競合検出はあるが、複数実行主体でのトランザクションDBの代わりにはならない。
- `shop_store.py` の注文状態更新はSQLiteの条件付き更新を利用するが、GitHubスナップショット方式を複数コンテナから同時に使うと、ローカルロックの範囲外で競合する可能性がある。
- PayPay暗号化キー、管理者認証、注文情報、引継ぎコード等は秘密情報。ソースコード・イメージ・ログに含めない。
- 現行 `render.yaml` はSingaporeリージョンで、Render専用の設定とパスを含む。

**結論:** 現行コードをそのままCloud Runへデプロイするだけでは不十分。Webと処理を分離し、永続状態・排他制御・再実行安全性をコンテナ外に移す設計が必要。

## 3. 目標アーキテクチャ案

### 3.1 Web / 注文受付

- Cloud Run Service、リージョン `asia-northeast1`（東京）。
- 最小インスタンス数0。アイドル時にスケール・トゥ・ゼロ。
- 初期は最大インスタンス数を小さく制限し、予算とデータ競合リスクを抑える。
- 管理画面・公開画面・注文受付・PayPay連携は現行UIとルートの互換性を維持する。
- 起動時に一時ファイルシステムへ永続データがあると仮定しない。Cloud Runのローカルファイルは一時領域として扱う。
- セッション署名鍵、PayPay暗号化鍵、GitHubトークン等はSecret Managerまたは安全な環境変数連携で注入する。値はログに出さない。

### 3.2 非同期実行

基本案:

1. 支払確認済みかつ資格情報提出済みの注文だけが実行要求を作成できる。
2. 注文DBで条件付き更新を行い、注文を「実行要求作成中」に一度だけ確保する。
3. Cloud Tasksへ注文IDとランダムな実行ID等の最小限の非秘密メタデータを送る。引継ぎコード、暗証番号、PayPayトークン、注文内容の平文をTask payloadやURLへ入れない。
4. Cloud Tasksのディスパッチ先は認証された起動用Cloud Run Service。起動用ServiceはCloud Run Jobsの実行を要求し、Job実行IDを永続DBに結び付ける。
5. Cloud Run Jobsは注文ID・実行IDを受け取り、DBから暗号化データを取得して権限確認後に処理する。秘密情報は必要な処理内だけで復号する。
6. Job終了時に成功・失敗・要手動確認を永続DBへ確定する。終了後にコンテナは破棄される。
7. 進捗は注文ID・実行ID単位で永続化し、WebはDBから読み取って表示する。

Cloud Tasksは起動要求の再配信が起き得るため、タスク名だけに依存せず、DB側の一意な実行ID・条件付き更新・状態遷移で二重起動を防ぐ。Cloud Tasksの成功応答は「Job起動要求の受理」を意味し、注文処理の完了を意味しない。

Cloud Run Jobsの最大実行時間、Taskのdispatch deadline、同時実行数、再試行、Job task timeout等は実装時点の公式仕様を再確認する。長時間処理がTaskのHTTP dispatch期限を超えないよう、TaskはJob起動要求のみを行い、BCSFE処理そのものをTaskのHTTPリクエスト内で実行しない。

### 3.3 状態保存・排他制御

**重要な設計ゲート:** 現行GitHub runtime-dataスナップショットはバックアップ・移行元として保全するが、複数Cloud Runインスタンスと複数Jobが同時更新する主DBとしてそのまま流用しない。

候補:

- Firestore Native mode等、トランザクション・条件付き更新・一意性を実現できるマネージドDB。
- 注文、決済台帳、実行要求、ジョブ状態、進捗、重複防止キーを一貫して管理できること。
- 料金、東京リージョン、無料枠の適用条件、バックアップ・エクスポート、アクセス制御を事前確認する。
- Firestoreの料金が月額1,000円以内に収まるかを実データ量・読み書き回数で見積もる。無料枠の適用を未確認のまま0円と断定しない。
- DBを採用できない場合、単一書き込み主体・キュー直列化を含む別案を設計し直し、GitHubスナップショットに複数Writerを接続するだけの案は採用しない。

GitHub runtime-dataの扱い:

- 現行 `runtime-data` ブランチと既存暗号化ファイルを変更・削除せず、移行前バックアップと照合を行う。
- `PAYPAY_ENCRYPTION_KEY` は絶対に変更・再生成しない。保存済みデータを復号できなくなるため。
- 移行時のDB変換は読み取り専用コピーから行い、件数・主キー・注文状態・決済digest・暗号化データの復号確認を行う。
- 移行後の新DBが正常と確認できるまで旧データを保持する。
- GitHub保存を監査用バックアップとして継続する場合も、書き込み頻度、Git履歴に残る暗号化スナップショット、API制限、競合・復旧方法を明示する。

### 3.4 Worker処理の移植

- 既存の `_run_job` / `_worker_entry` / BCSFE初期化・データ準備・スロット管理を調査し、Webアプリのimport副作用から分離する。
- Cloud Run Jobの1実行が1注文（1 execution ID）を処理する方式を基本とする。
- 既存の同時実行上限4は初期値として固定せず、Jobの並列数・BCSFEディスク使用量・メモリ・CPU・外部通信を計測して決定する。
- 同一アカウント・同一引継ぎコードの重複処理防止を永続DBの排他制御に移す。
- BCSFEの一時ファイル・キャッシュ・スロットディレクトリはJobごとに分離し、終了時に破棄可能とする。
- カタログやゲームデータの取得・キャッシュは起動時間と外部通信量を計測し、毎回のJob起動で不要な重複取得をしない設計を検討する。
- 進捗・警告・結果を永続DBへ安全に逐次保存し、Job再起動・Webスケールイン後も照会できるようにする。
- Job失敗・タイムアウト・結果不明時に、ゲーム側で処理済みの可能性がある操作を自動再実行しない。要手動確認へ移す。

## 4. データ・セキュリティ要件

- 注文データ、引継ぎコード、暗証番号、PayPay認証情報は保存時暗号化を維持する。
- Cloud Tasks payload、URL、ログ、例外トレース、Cloud Run Jobの引数に秘密情報を含めない。
- Cloud Run Service、Job、Task起動用サービスには最小権限の専用サービスアカウントを使う。
- Cloud Tasksから起動用Serviceへの認証にはOIDCを使用し、一般公開しない。
- Job実行権限、Secret Manager読み取り権限、DBアクセス権限を用途別に分離する。
- GitHubトークンはベータ専用・最小権限。必要性がなくなればランタイムから外す。
- PayPayの非公式Webプロトコルは仕様変更・利用制限のリスクがある。Cloud Run移行で認証制限を回避する目的のプロキシ・偽装・地域制限回避は実装しない。東京リージョンでの応答差は検証結果として扱い、成功を保証しない。
- 決済の不確定結果、二重受取、同一リンク再利用は現行どおり自動再試行せず、管理者の手動確認へ回す。
- 注文受付の `SHOP_LIVE_ORDERS_ENABLED=0` を維持し、管理者による検証承認前に有効化しない。

## 5. 予算設計（月額1,000円以内）

目標は無料枠内運用を基本とし、少額の従量料金を含めても1,000円以内。

見積対象:

- Cloud Run Service（CPU、メモリ、リクエスト、最小インスタンス0）。
- Cloud Run Jobs（CPU・メモリの実行秒数、同時実行数）。
- Cloud Tasks（タスク操作数）。
- Firestore等の主DB（読み書き、保存容量、ネットワーク、バックアップ）。
- Artifact Registry（イメージ保存容量・転送）。
- Secret Manager（シークレットバージョン・アクセス）。
- Cloud Logging（取り込み・保存・ログ量）。
- 外向きネットワーク通信、東京リージョンの外部APIアクセス。
- Cloud Scheduler等の定期処理を追加する場合の料金。

必須対策:

- Google Cloud Billing予算アラートを設定する。予算アラートは自動停止保証ではないことを明記する。
- Cloud Run Serviceの最大インスタンス数、Job並列数、Task dispatch rateを制限する。
- Cloud Loggingのノイズを抑え、秘密情報を記録しない。
- 料金計算ツールで通常月・注文集中月・障害再試行月の3ケースを試算する。
- 1,000円を超える見込み、または無料枠の適用条件が不明な場合は本採用を保留する。
- 無料枠・料金はリージョン、SKU、課金方式、アカウント条件で変わるため、実装時点の公式料金表で再確認する。

## 6. 移行フェーズと完了条件

### Phase 0: 現状固定・バックアップ

- Renderの現行サービス・環境変数名・デプロイコミット・設定を記録。
- GitHub `runtime-data` ブランチの全ファイル一覧、blob SHA、サイズを記録。
- 暗号化キーの保管者・復旧手順を確認。秘密値はドキュメントに書かない。
- 注文受付は停止状態を維持。
- Renderサービスを停止・削除しない。

完了条件: 復旧可能なバックアップと環境一覧があり、秘密情報を含まないこと。

### Phase 1: アーキテクチャ・料金の確定

- Firestore等の主DB候補を決め、東京リージョンと料金を検証。
- Cloud Tasks + 起動用Service + Cloud Run Jobsの実現可能性を確認。
- Jobの最大実行時間・メモリ・CPU・一時ディスク容量を見積もる。
- 無料枠を含む月額予算表を作成。
- GitHub runtime-dataを主DBとして使わない場合の移行・バックアップ仕様を決める。

完了条件: 1,000円以内の根拠があり、データ整合性と再実行防止の設計レビューが完了。

### Phase 2: アプリ分離とテスト

- Webルート・注文状態遷移・PayPay gateway・BCSFE実処理を独立モジュールへ整理。
- Cloud Tasksの重複配送、Jobの重複起動、Job失敗、タイムアウト、途中停止をモックでテスト。
- DBの条件付き更新、注文ごとの一意な実行ID、同一引継ぎコード排他をテスト。
- 進捗・結果・手動確認状態がコンテナ終了後も残ることをテスト。
- PayPayの実アカウントを使わないモック試験を優先する。

完了条件: CIと回帰テストが通り、重複実行・データ消失・秘密情報ログ漏えいがない。

### Phase 3: 東京の隔離テスト環境

- Renderとは別のGoogle Cloudプロジェクトまたは明確に分離したリソースで作成。
- 東京リージョンにService、Tasks、Jobs、DBを構築。
- テスト用Secret・専用サービスアカウントを使用。
- 注文受付・PayPayライブ処理は無効のまま、UI・管理者認証・ヘルスチェック・Workerのモック実行を確認。
- Cloud Runがアイドル時に0インスタンスになること、注文処理後にJobが終了することを確認。
- ログ・メトリクス・請求データで費用を測定。

完了条件: Renderに影響せず、東京環境の基本動作・自動停止・費用が確認できる。

### Phase 4: データ移行リハーサル

- runtime-dataの読み取り専用コピーから移行。
- 注文、決済台帳、PayPay暗号化情報、管理者認証データの移行対象を明確化。
- 注文件数・状態・一意制約・暗号化データの復号可否を照合。
- 移行中に新旧環境が同じ注文を更新しないよう、書き込み主体を一つに限定。
- ロールバック手順とデータ差分の回収手順を確認。

完了条件: 移行前後の件数・状態が一致し、復旧試験が成功。

### Phase 5: 限定的な実機検証

- PayPayの利用条件・公式サポートに照らして検証可能な方法を確認。
- ユーザーの明示的な承認がある場合に限り、少額・限定的な実機テストを実施。
- PayPayのログイン成功だけでなく、受取・注文状態・処理・結果・手動確認フローを検証。
- 不確定な決済・ゲーム操作を自動再試行しない。
- 失敗時はRenderへ戻せる状態を維持。

完了条件: 実機テスト結果、セキュリティ、費用、運用手順が確認されている。

### Phase 6: 切替判断

- 全テスト・費用・復旧条件を確認してから切替可否を判断。
- ユーザーの明示的な承認なしにRenderを停止・削除しない。
- 切替後も一定期間Render設定とデータバックアップを保持。
- 安定版 `CatsDaiko5` は一切変更しない。

## 7. 必須テスト一覧

- 公開ページ・管理画面・ログイン・権限境界。
- 注文作成、価格（50円/体・25円/体）、数量上限、サーバー側価格計算。
- PayPay未確認・金額違い・期限切れ・重複・不確定結果のfail-closed動作。
- 決済前に引継ぎ情報を入力・参照できないこと。
- 注文トークンが他注文へアクセスできないこと。
- Cloud Tasksの重複配送・遅延・失敗時にも一注文一実行であること。
- Cloud Run Jobの起動失敗・タイムアウト・強制終了時に安全な状態へ遷移すること。
- 同一アカウント・引継ぎコードの重複実行防止。
- Worker進捗・結果がWebコンテナ再起動後も確認できること。
- 複数Webインスタンス・複数Jobからの同時DB更新。
- GitHub runtime-dataバックアップ・復元・照合。
- 暗号化キー不一致、DB障害、GitHub API制限時にfail-closedで停止すること。
- ログにパスワード、Cookie、OTP、PayPay token、引継ぎコード、暗証番号が出ないこと。
- アイソレーション、OIDC認証、サービスアカウント最小権限。
- Cloud Run Serviceがアイドル時に0へスケールすること。
- Job終了後に処理コンテナが常時稼働しないこと。
- Billingで月額1,000円以内の見込みを確認すること。

## 8. ロールバック方針

- Render版を移行期間中の復旧先として維持する。
- 新旧環境の同時書き込みは禁止。切替時は注文受付を一時停止し、DBの書き込み主体を明確に切り替える。
- 新環境で決済状態が不明な注文は自動で旧環境へ戻して再処理しない。PayPay側の状態を照合し、手動確認する。
- DB移行後の差分を旧環境へ戻す場合は、二重処理・状態巻き戻りが起きない移行手順を用意する。
- 問題があれば新環境の注文受付を停止し、Renderへ戻す。ただしデータ差分を照合するまで旧環境で注文・決済を再開しない。

## 9. 今回の作業範囲と未実施事項

このコミットで行うのは**計画書の追加のみ**。

未実施:
- Cloud Runのプロジェクト・リソース作成
- GCP課金アカウントの設定
- Cloud Tasks / Jobs / Firestoreの作成
- Dockerfile、Cloud Build、Terraform等の追加
- アプリケーションコード変更
- runtime-dataの移動・変更
- Secretの移動・再発行
- Render環境変数・デプロイの変更
- PayPayログイン・決済の実機テスト
- 注文受付の有効化
- Renderサービスの停止・削除

## 10. 公式資料（実装時に再確認）

- Cloud Run pricing: https://cloud.google.com/run/pricing
- Cloud Run locations: https://cloud.google.com/run/docs/locations
- Cloud Run scale to zero: https://cloud.google.com/run/docs/about-instance-autoscaling
- Cloud Run Jobs: https://cloud.google.com/run/docs/create-jobs
- Cloud Run Jobs execution: https://cloud.google.com/run/docs/execute/jobs
- Cloud Tasks pricing: https://cloud.google.com/tasks/pricing
- Cloud Tasks HTTP target: https://cloud.google.com/tasks/docs/creating-http-target-tasks
- Firestore pricing: https://cloud.google.com/firestore/pricing
- Secret Manager pricing: https://cloud.google.com/secret-manager/pricing
- Google Cloud budgets: https://cloud.google.com/billing/docs/how-to/budgets

---

**運用上の原則:** 安さよりも注文・決済・セーブデータの整合性を優先する。無料枠に収まらない設計を無理に採用せず、1,000円以内を満たせない場合は実装前に代替案と費用を提示する。


## 11. Phase 1 調査結果（2026-11-13）

### 11.1 公式仕様で確認できたこと

- Cloud Runは東京 `asia-northeast1` に対応しており、東京はTier 1料金リージョン。Singapore（`asia-southeast1`）はTier 2。東京へ配置すること自体は可能。
- Cloud Run Serviceのリクエストベース課金は、リクエスト処理中・起動・シャットダウン時が主な課金対象。最小インスタンス数0なら、通常のアイドル時間にインスタンスを維持する料金を避けられる。
- Cloud Runの無料枠（リクエストベースService）は、月あたりCPU 180,000 vCPU秒、メモリ360,000 GiB秒、リクエスト200万回。Jobは別区分でCPU 240,000 vCPU秒、メモリ450,000 GiB秒の無料枠が表示されている。無料枠は請求先アカウント単位でプロジェクト横断集計されるため、他プロジェクトでの利用分も影響し得る。
- Cloud Run Jobsは実行中のインスタンス時間に対して課金され、最低課金時間は1分。Jobが終了すれば処理インスタンスは常時稼働しない。
- Cloud Tasksは月間最初の100万billable operationsが無料。操作数にはAPI呼び出しとpush delivery attemptが含まれ、32KB単位で計算される。リトライやタスク操作が増えると無料枠を消費する。
- Firestore Standardには、1プロジェクトにつき1データベースのみ適用される無料枠がある。保存1GiB、読み取り50,000/日、書き込み20,000/日、削除20,000/日、外向き転送10GiB/月。無料枠は日次リセット。TTL削除、PITR、バックアップ、復元、クローン等は無料枠対象外。
- Firestore Native modeとCloud Runの双方で東京 `asia-northeast1` が利用可能。

### 11.2 暫定費用判断

**現時点の判断: 月額1,000円以内を狙える構成ではあるが、まだ保証できない。**

理由:
- Web Serviceをmin instances 0、request-based billingで運用し、アクセスが少なければServiceの課金を無料枠内に抑えられる可能性がある。
- Cloud Tasksは100万操作/月まで無料枠がある。
- Firestoreは小規模な注文・進捗データなら、無料枠内に収まる可能性が高い。
- Cloud Run Jobsも月間実行時間が少なければ無料枠内に収まる可能性がある。
- ただし、Cloud Runの無料枠はリージョンTier・請求先アカウント内の他利用量・ServiceとJobの課金区分に左右される。Artifact Registry、Cloud Build、Secret Manager、Cloud Logging、外向き通信なども別途確認が必要。無料枠超過やログ大量出力、Jobの長時間実行・再試行で課金が発生する。

現段階で月額を確定できない理由:
- 1件あたりBCSFE処理時間、CPU・メモリ使用量、月間注文件数が未計測。
- コンテナイメージのサイズ・保持数、ビルド頻度、ログ量が未計測。
- Firestoreへ移すデータ量・1注文あたりのread/write回数が未設計。
- Google Cloud請求先アカウントの無料枠消費状況が不明。

### 11.3 Phase 1の暫定アーキテクチャ決定

次の構成を第一候補として進める。

`Cloud Run Web Service (Tokyo, min=0, request-based)`
→ `Cloud Tasks`
→ `Private authenticated dispatcher Service`
→ `Cloud Run Jobs (per order/execution)`
→ `Firestore Standard (Tokyo)`

- Firestoreを注文・実行状態のトランザクションDB候補とする。SQLiteからのデータモデル移行が必要なため、単純なファイル置換ではなくRepository層を設ける。
- GitHub `runtime-data` はバックアップ・移行元として維持し、Cloud Runの複数Writerから直接更新する主DBにはしない。
- Cloud Tasks payloadは注文ID・実行ID等の非秘密メタデータに限定する。
- DispatcherはJob起動要求だけを行い、BCSFE処理をHTTPリクエスト内で実行しない。
- Jobは注文単位で起動し、DB上の一意な実行ID・条件付き状態遷移で重複配送と二重実行を防ぐ。
- Cloud Run Serviceはmin instances 0、最大インスタンス数を制限。Jobの並列数・再試行数も小さく制限する。
- 予算アラートを設定するが、アラートは自動停止機能ではないため、別途利用上限・インスタンス数・Job並列数・ログ量を制限する。

### 11.4 まだ決めていないこと・次の調査

1. FirestoreへのSQLiteデータモデル移行方法。特に注文、決済台帳、暗号化済み資格情報、実行ロック、進捗、結果の整合性設計。
2. Firestoreのインデックス・トランザクション設計と、実データ量に基づくread/write見積もり。
3. Artifact Registry、Cloud Build、Secret Manager、Cloud Logging、ネットワーク転送の東京リージョン適用料金。
4. Cloud Run JobのCPU・メモリ・一時ディスク・実行時間の実測。既存Render環境のログや処理記録から安全に推定できるか確認する。
5. Google Cloud Pricing Calculatorで通常月・注文集中月・異常再試行月の3ケースを算出する。
6. 予算上限を超えた場合の安全策。課金アラートだけに頼らず、注文受付停止・Job起動停止の運用スイッチを設計する。

### 11.5 今回の変更範囲

この追記は公式料金・リージョン・無料枠の調査結果と暫定設計を記録したもの。GCPリソース作成、課金有効化、アプリコード変更、Render設定変更、注文受付の有効化、PayPay実機テストは行っていない。Renderと安定版 `CatsDaiko5` は変更していない。

### 11.6 Phase 1で参照した公式資料

- Cloud Run pricing: https://cloud.google.com/run/pricing
- Cloud Run locations: https://cloud.google.com/run/docs/locations
- Cloud Run billing settings: https://cloud.google.com/run/docs/configuring/billing-settings
- Cloud Run Job task timeout: https://docs.cloud.google.com/run/docs/configuring/task-timeout
- Cloud Tasks pricing: https://cloud.google.com/tasks/pricing
- Firestore pricing: https://cloud.google.com/firestore/pricing
- Firestore quotas and free tier: https://docs.cloud.google.com/firestore/quotas
- Firestore Native mode locations: https://docs.cloud.google.com/firestore/native/docs/locations



## 12. Phase 1 追加調査：ビルド・秘密情報・ログ費用（2026-11-13）

### 12.1 追加で確認した公式無料枠・料金

- Artifact Registry: 請求先アカウント単位で合計0.5GiBまで保存無料。超過分は保存量に応じて課金。東京のCloud Runと同じリージョンにリポジトリを置けば、同一リージョン内のGoogle Cloudサービスへの転送は無料扱い。古いイメージを残し続けない。
- Cloud Build: 請求先アカウントあたり月2,500 build-minutesの無料枠。無料枠の対象はデフォルトプールのe2-standard-2。ビルド待ち時間はbuild-minuteに含まれない。ビルド頻度とビルド実行時間を監視する。
- Secret Manager: 請求先アカウントあたり月6個のアクティブsecret versions、月10,000回のaccess operationsまで無料。超過時はアクティブバージョン数とアクセス回数に応じて課金。秘密値を毎リクエスト取得せず、インスタンス起動時に必要なバージョンを読み込む設計を検討する。既存のPAYPAY_ENCRYPTION_KEYは値を変更・再生成せず、移行時に同一値を安全に移す。
- Cloud Logging: 通常のログ取り込みはプロジェクトあたり月50GiBまで無料。超過分は$0.50/GiB。標準保持期間を超える追加保持は別料金。不要なdebugログ・資格情報を含むログを抑制し、ログ量と保持期間を制限する。

### 12.2 予算への影響と運用方針

- 小規模な単一ベータ環境で、Artifact Registryのイメージを0.5GiB以内、Cloud Buildを2,500分以内、Secret Managerを6アクティブバージョン・10,000アクセス以内、Loggingを50GiB/月以内に抑えれば、これら4項目は公開されている無料枠に収まる可能性がある。
- これらの無料枠も請求先アカウント単位で他プロジェクトと共有される項目がある。既存利用分が不明なため、アカウント全体の残枠を確認する必要がある。
- Artifact Registryの脆弱性スキャン等の追加機能、外向き通信、ネットワーク転送、ログ転送先、無料枠超過は別料金になり得る。
- 画像タグは固定タグと最新1～2世代を基本にし、古いイメージを定期的に削除する運用を設計する。
- Cloud Loggingは注文ID・実行IDなどの追跡用メタデータに限定し、PayPay Cookie/token、OTP、パスワード、引継ぎコード、暗証番号、復号済み注文情報を出力しない。
- Secret Managerへのアクセスは専用サービスアカウントに必要なsecret単位で付与し、管理者権限をアプリに与えない。

### 12.3 Phase 1の現在地

東京リージョンと主要コンポーネントの無料枠は確認できた。引き続き、Cloud Run Jobsの実行コストとFirestoreのread/write回数が支配的な費用要因になる可能性がある。現行環境の処理時間・注文量・データ量が分からないため、現時点では1,000円以内の確定見積もりには至っていない。

次の作業は、現行Betaの注文・Worker・SQLiteデータモデルをコード上でさらに調査し、Firestore移行に必要なRepository境界、トランザクション、読み書き回数を設計すること。その後に3つの利用シナリオで費用試算する。

### 12.4 追加参照した公式資料

- Artifact Registry pricing: https://cloud.google.com/artifact-registry/pricing
- Cloud Build pricing: https://cloud.google.com/build/pricing
- Secret Manager pricing: https://cloud.google.com/secret-manager/pricing
- Cloud Logging pricing: https://cloud.google.com/logging/pricing



## 13. Phase 1 追加調査：現行コードのDB・Worker境界（2026-11-13）

### 13.1 注文DBの現状

`shop_store.py` を確認。ショップDBは現在SQLiteの `orders` テーブル1つを中心に構成され、主な列は次のとおり。

- 識別・権限: `id`, `access_hash`
- 商品・金額: `sku`, `title`, `amount`, `unit_price_yen`, `quantity`, `service_type`
- 暗号化データ: `credential_enc`, `operation_enc`
- 決済: `status`, `payment_digest`（UNIQUE）, `payment_note`
- 処理管理: `credentials_submitted`, `job_id`
- 時刻: `created`, `updated`

注文状態の遷移はコードで制限されている。主な状態は `awaiting_payment` → `payment_processing` → `paid` → `fulfilling` → `completed`。異常時の `review`、キャンセル `cancelled` もある。決済digestの一意性と条件付き更新は移行後も維持が必要。

`credential_enc` には引継ぎコード・暗証番号、`operation_enc` には選択内容がFernet暗号化されて保存される。Firestore移行時も暗号文をそのまま保存し、既存のFernet鍵を変更しない。アクセス用tokenそのものではなくSHA-256 hash（`access_hash`）を保存する設計も維持する。

### 13.2 現行Workerのプロセス内状態

`app.py` では次の状態がメモリ・ローカルファイルに依存している。

- `_jobs`: Jobの進捗・ログ・警告・結果・所有者等。
- `_pending_jobs`: 待機キュー。注文資格情報を含む引数tupleもメモリに置かれる。
- `_slots`: 同時実行枠とwarmup枠。
- `_active_transfers`: 引継ぎコード重複実行防止用のプロセス内set。
- `_jobs_lock`, `_run_lock`, `_init_lock`: Pythonプロセス内のロック。
- `fcntl.flock`: コンテナのローカルファイルシステム上のロック。
- Jobの進捗はPipeで親プロセスへ送られ、Web側の `_jobs` に反映される。結果保持は30分、待機期限は15分。
- BCSFEの準備済みデータ、キャッシュ、名前カタログ、ゲームバージョンの一部は `/tmp` とプロセスメモリにある。

Cloud Runの別Service・別Job・別コンテナ間でこれらのメモリ・ローカルロックを共有することはできない。Cloud Runへ単純に載せ替えると、進捗消失、重複実行、同時実行上限の破綻、コンテナ終了による状態消失が起こり得る。

### 13.3 必要なFirestoreデータモデル（設計案）

SQLiteの1注文レコードをそのまま1ドキュメントへ移すだけでは不十分。最低限、次の論理データを分離する。

1. `orders/{order_id}`: 注文情報、金額、商品、状態、暗号化済み資格情報・操作内容、access hash、決済digest、時刻。
2. `executions/{execution_id}`: 注文との関連、Job状態、step、進捗、短いログ・警告・結果、開始・終了時刻、outcome_unknown。
3. `transfer_locks/{hmac_digest}`: 引継ぎコードの重複実行ロック。平文コードはキー・ログ・ドキュメントIDに使わず、鍵付きHMAC digestを使う。lease owner・期限・実行IDを保存し、トランザクションで取得・更新・解放する。
4. `worker_slots/{slot_id}`: 全Jobを横断する同時実行枠の予約。最初は4枠を上限候補とし、実機測定で決める。取得・解放・期限管理をFirestoreトランザクションで行う。
5. 必要に応じて `task_outbox/{task_id}`: DB状態更新とCloud Tasks投入の間で片方だけ成功する問題を防ぐOutbox。再送可能な状態を保存し、dispatcherが冪等に送信する。

実際のコレクション分割・サブコレクション・インデックスは、Firestoreのトランザクション制約とアクセスパターンを整理してから確定する。Jobのログはサイズ・件数上限を設け、1ドキュメントのサイズ上限に達しないよう分割・要約する。決済・注文状態の監査に必要な履歴は、単一の可変フィールドで上書きせず、追記型イベントとして保存する案を検討する。

### 13.4 Cloud Tasks / Jobの同時実行制御に関する重要点

Cloud Tasksの `maxConcurrentDispatches` はDispatcherへの同時HTTP dispatchを制限する設定であり、DispatcherがJob起動後すぐに成功応答を返す設計では、実行中Cloud Run Jobsの総数を直接制限できない。

そのため、既存の `MAX_CONCURRENT_JOBS=4` 相当の制限を守るには、DispatcherとJobの間でFirestore上の共有slot leaseを取得する必要がある。slotが取れない場合のCloud Tasks再試行、Job終了時のslot解放、Job異常終了時のlease回収・状態照合を設計する。lease期限切れだけを理由に処理中Jobのslotを即時再利用すると、元Jobがまだ動いている場合に重複処理となるため、Cloud Run Job execution状態と照合する回復処理が必須。

### 13.5 実装時に守るべき境界

- 注文受付Webは引継ぎコード・暗証番号をTask payload、Job引数、URL、ログへ載せない。
- Jobは `order_id` と `execution_id` のみを起点にDBから対象注文を取得し、実行権限・支払済み・資格情報提出済み・execution所有者を再検証してから復号する。
- 既存の `claim_payment` / `complete_payment` / `claim_execution` / `set_status` の状態遷移をFirestoreトランザクションまたは条件付き更新で再現する。
- 決済処理とゲーム処理は同じ再試行ポリシーにしない。決済状態不明やゲーム側のコード発行結果不明は、自動再実行せず `review` / `outcome_unknown` へ移す。
- Webの `/status/<job_id>` はメモリ上のJob辞書ではなく永続DBのexecution状態から返す。セッションのowner照合も永続データと合わせて行う。
- Jobは注文単位で実行するが、ユーザー操作のキャンセル・進捗確認は永続状態とCloud Run Job execution制御の両方を扱う。
- `/tmp` のBCSFEキャッシュはJob間で共有できない。コンテナイメージへ含める方法、Cloud Storage等の共有キャッシュ、毎回取得する方法を、イメージサイズ・起動時間・転送費用・ゲームデータ更新頻度で比較する。

### 13.6 現時点の評価

Firestoreを主DBにする場合、ショップのSQLite利用箇所だけでなく、Workerの進捗・キュー・同時実行枠・重複防止をまとめて移行する必要がある。特に決済とゲーム実行の結果不明状態を安全に表現することが最重要。単純なDBアダプター差し替えだけでは完成しない。

この調査はコード読解のみ。アプリコード、Render、runtime-data、注文受付設定、PayPay設定は変更していない。



## 14. Phase 1 追加調査：PayPay・管理者認証の永続データ（2026-11-13）

### 14.1 PayPayデータの現状

paypay_gateway.py を確認。PayPay連携にはショップ注文DBとは別に次の状態がある。

- paypay/account.enc: PayPayアカウント情報（電話番号、パスワード、UUID、access token、session cookie、有効期限等）をJSON化し、Fernet暗号化したBlob。GitHub runtime storeが有効なRender環境ではGitHub runtime-dataに保存。
- paypay-ledger.sqlite3: linksテーブル。送金リンクdigest（PRIMARY KEY）、order_id（UNIQUE）、state、createdを保持し、attempted / paid / pending / review等で重複受取・不確定結果を管理。GitHub runtime store経由の暗号化SQLiteスナップショットとして保存。
- _pending_otp: SMS認証中の電話番号・パスワード・UUID・OTP reference・prefix・Cookieをプロセスメモリに保持。プロセス再起動やCloud Runのスケールダウンで失われる。
- _LOCK: PayPayログイン・受取処理を保護するPythonプロセス内threading.Lock。別コンテナ間では共有されない。

### 14.2 Cloud Run移行で追加必須となる対応

- PayPayアカウントの暗号化データを移行する場合、PAYPAY_ENCRYPTION_KEYを現行と同じ値でSecret Managerへ安全に登録する。鍵の再生成は禁止。保存済みaccount.encを読み戻せることをテストする。
- PayPay受取台帳は注文DBと別の整合性境界として移行し、リンクdigestとorder_idの両方の一意性をFirestore transactionで保証する。
- PayPayの受取要求は外部API呼び出しとDB書き込みの間に分散トランザクションが存在しない。送信前にdigestをattemptedとして確保し、応答不明・タイムアウト・プロセス終了時はreview/pendingへfail-closedで移す。移行後も同一リンクの自動再送は禁止。
- _pending_otpを単なるプロセスメモリに残さない。SMS認証フローを継続する必要がある場合、必要最小限の情報を暗号化して永続化し、短いTTL・一回限りの状態遷移・管理者セッション所有確認を実装する。OTPそのものは保存・ログ出力しない。認証フローの有効期限切れ時は再開せず最初からやり直す。
- _LOCKは分散ロックに置き換える。PayPayアカウントのrefresh token/session更新と受取処理の競合を避けるため、アカウント単位のlease/transactionを設計する。期限切れleaseの回収は、外部API操作の結果が不明な状態を誤って再実行しないよう慎重に行う。
- PayPayアカウント情報にはパスワード・Cookie・token等の強い秘密情報が含まれるため、Firestoreへ保存する場合はFernet暗号文のみを格納し、IAM最小権限・ログ除外・バックアップ暗号化を徹底する。Secret Managerを動的セッション台帳として使うのではなく、長期暗号化キーや固定シークレットの保管に用いる。
- PayPayログイン・SMS認証の失敗をCloud Runのリージョン変更で回避できるとは断定しない。利用条件と公式サポートの範囲内で検証する。

### 14.3 管理者認証の現状

- render.yamlはAUTH_GITHUB_REPO / AUTH_GITHUB_TOKENを設定する構成で、アプリはGitHubPasswordStoreを利用する。
- github_auth_store.pyはパスワードhashをGitHubリポジトリ内のファイルへ保存する。これはruntime-dataの注文DBとは別の保存経路。
- auth_store.pyはローカルSQLiteを使うfallback実装。Cloud Runのローカルディスクは永続ではないため、fallbackへ意図せず切り替わらないよう起動時設定検証を行う。
- 管理者パスワードhashをFirestoreへ移す必要があるかは、現行GitHubPasswordStoreをCloud Runから継続利用する場合と比較して決める。GitHubファイル保存を残す場合も、同時更新競合とGitHub API障害時のfail-closed動作を確認する。

### 14.4 移行対象データ一覧の更新

Firestore移行対象候補は、少なくとも次の4領域となる。

1. ショップ注文 orders（shop.sqlite3）
2. PayPay受取リンク台帳 links（paypay-ledger.sqlite3）
3. PayPayアカウント暗号化Blob（paypay/account.enc）
4. Worker execution / progress / queue / transfer lock / slot lease（現状は主にプロセスメモリ）

管理者パスワードhashは現行GitHubPasswordStoreを維持するか、別途移行を決定する。既存runtime-dataの全ファイル一覧・blob SHA・暗号化状態をPhase 0で取得してから、対象と件数を確定する。

この調査はコード読解のみ。PayPayログイン・受取は実行していない。秘密値、token、Cookie、OTPは取得・記録・表示していない。アプリコード、Render、runtime-data、注文受付設定は変更していない。



## 15. Phase 1 設計：Firestoreデータモデルと整合性（2026-11-13）

### 15.1 設計原則

- Firestore Native mode / Standard edition / Tokyo asia-northeast1を候補として固定する。
- 注文・決済・executionの状態変更はFirestore transactionを使い、外部API呼び出し中にtransactionを開いたままにしない。
- transaction callbackは競合時に再実行され得るため、callback内でPayPay API、Cloud Run Jobs API、Cloud Tasks API、メール送信、ゲームデータ変更などの外部副作用を起こさない。
- Firestoreは単一ドキュメント1MiB上限とtransaction request 10MiB上限を前提に設計する。長いログや大きなゲームデータ・BCSFEキャッシュはFirestoreに格納しない。
- 秘密情報はFernet暗号文のまま保存し、平文をドキュメントID・Cloud Tasks payload・Job引数・URL・ログへ出さない。
- 注文状態の監査履歴は追記型イベントにし、現在状態のドキュメントとは分離する。
- Cloud Run Jobの初期max retriesは0を候補とする。ゲーム処理は外部状態変更の結果が不明なまま自動再試行すると二重実行の危険があるため、処理段階ごとの安全な再開仕様ができるまで自動retryを有効化しない。

### 15.2 コレクション案

| Collection / document | 主な内容 | 書き込み責任 |
|---|---|---|
| orders/{order_id} | 現行ordersの列。amount・商品・数量・status・access_hash・暗号化credential_enc/operation_enc・payment_digest・job/execution ID・時刻 | Web/API |
| order_events/{event_id} | order_id、from/to state、event type、actor、created、短い非秘密metadata | 状態遷移transaction |
| payment_attempts/{link_digest} | digest、order_id、attempted/paid/pending/review/outcome_unknown、provider status code等の安全なmetadata、created/updated | 決済処理 |
| executions/{execution_id} | order_id、state、step、progress、owner、lease expiry、Cloud Run Job execution name、started/heartbeat/finished、outcome_unknown | Dispatcher / Job |
| execution_logs/{execution_id}/chunks/{chunk_id} | 件数・サイズ上限付きの短いログchunk、時刻、severity。秘密情報・資格情報禁止 | Job |
| transfer_locks/{hmac_digest} | 引継ぎコードの鍵付きHMAC、owner execution、lease expiry、state | Job |
| worker_slots/{slot_id} | slot number、execution_id、owner、lease expiry、state | Job |
| task_outbox/{task_id} | execution_id、task type、pending/dispatching/dispatched/failed、attempt count、lease | Web transaction / Dispatcher |
| paypay_accounts/{account_id} | account.encと同等のFernet暗号文、version、updated。1管理者アカウントを想定 | 管理者認証処理 |
| admin_sessions/{session_id}（必要時） | OTPフロー等の短命な暗号化状態とexpiry。通常のWebセッションを無条件にFirestoreへ移さない | 管理者認証処理 |

ID設計:
- order_idは現行の10文字英数字を維持し、公開アクセスtokenそのものではなくSHA-256 hashのみ保存。
- payment_attemptsのdocument IDはPayPayリンク文字列そのものではなく、既存同様のSHA-256 digestを使用。ログにdigest全文を出さない。
- transfer_locksのdocument IDは引継ぎコードの平文SHA-256ではなく、Secret Managerの専用pepperを使うHMAC-SHA256。引継ぎコードを推測する辞書攻撃への耐性を確保する。
- execution_id / task_idは暗号学的乱数で生成。注文ごとのexecution作成はtransactionで一意にclaimし、再送時は既存executionを返す。

### 15.3 重要なtransaction境界

A. 注文作成:
1. サーバーで商品・数量・価格を再検証し、暗号化credential/operationを作成。
2. orders/{id}とorder_eventsのcreatedを同一transactionで作成。
3. 同じ注文IDが既に存在する場合は新規作成扱いにしない。

B. 決済claim:
1. PayPayリンクを正規化し、SHA-256 digestを算出。
2. transaction内で注文がawaiting_paymentであること、payment_attempts/{digest}が存在しないことを確認。
3. ordersをpayment_processingへ変更し、payment_attemptsをattemptedで作成し、order_eventを追加。
4. transaction commit後にのみPayPay外部APIを呼び出す。
5. PayPay受取成功の明確な応答が得られた場合、transactionでpayment_attemptsをpaid、ordersをpaid、eventを追記する。
6. タイムアウト・HTTP応答不明・プロセス終了・DB確定失敗時はreview/outcome_unknownへfail-closed。成功可否が確認できないリンクを自動再送しない。外部API成功後にDB書き込みだけ失敗した場合も同じ扱いにし、管理者確認・照合を必須とする。
7. 同じdigestまたは同じorder_idを別注文に使えない一意性を維持する。FirestoreはRDBのUNIQUE制約がないため、digestをdocument IDにしてtransactionで存在確認する。

C. 引継ぎ情報保存:
1. 注文所有tokenのSHA-256を照合し、注文がpaid、未提出、execution未開始であることをtransaction内で確認。
2. credential_encをFernet暗号文で保存し、credentials_submitted=1に変更。
3. 平文transfer code / PINをFirestoreの他フィールド・event・ログへ複製しない。

D. execution enqueue:
1. transaction内で注文がpaid、credentials_submitted=1、active executionなしを確認。
2. executions/{id}をqueuedで作成し、orders.active_execution_idを設定し、task_outbox/{task_id}をpendingで作成。
3. transaction commit後、Cloud Tasks dispatcherがoutboxを送信する。DB更新とCloud Tasks作成の間でクラッシュしてもoutboxを再走査して再送できる。
4. Cloud Tasksは少なくとも1回配送される前提。task_id / execution_idによる冪等処理を行う。

E. Job claim:
1. Jobはorder_id / execution_idのみを引数として起動し、DBから状態を再取得。
2. transactionでexecutionがqueued、注文がpaid、資格情報提出済み、active_execution_id一致を確認。
3. worker_slotsの空きを読み取り、executionとslotを同じtransactionでrunning/owner/lease付きに更新。transfer lockも同時にclaimする。
4. slotが空いていない場合は処理を始めない。Dispatcher側の再スケジュールと待機キューで制御し、起動したJobを長時間sleepさせない。
5. 既に別Jobがclaim済みならこのJobは副作用を起こさず終了する。

F. execution完了:
1. step更新・進捗・heartbeatをDBへ反映するが、ログ1行ごとに書かず一定時間または段階ごとにまとめる。
2. 完了結果を確定できた場合、execution completedとorder completedをtransactionで更新し、slot/transfer lockを解放しeventを追加。
3. 失敗・キャンセル・結果不明では、外部処理が未実行と証明できない限りqueuedへ戻さず、review/outcome_unknownへ移して自動再実行を禁止。
4. heartbeat消失だけでslotを回収しない。Cloud Run Job executionの実際の状態を照合し、終了済みであることを確認してからreaperが回収する。

### 15.4 Cloud Run Job / Cloud Tasksの初期制限

- 初期Jobは1 executionあたり1 task、parallelism=1。MAX_CONCURRENT_JOBS=4を維持する場合はFirestore worker_slots 4枠をglobal gateとして使う。
- Cloud Run Jobのmax retries=0から開始し、失敗時はexecutionをreviewへ。副作用のない初期化工程のみ再試行可能と証明できた段階で工程単位のretryを再検討。
- Cloud Run Job task timeoutは初期値を短く決め打ちしない。Render実測のP50/P95/P99と最長処理時間を取得し、その最大値に余裕を足して設定する。Cloud Run Jobsの公式上限は1 taskあたり最大168時間だが、長いtimeoutは無停止実行を保証しない。
- Cloud Tasksのdispatch concurrencyはDispatcherへの同時HTTP配信制御に限定し、Job実行中の総数制御はFirestore slot leaseで行う。
- Dispatcherは短時間で応答し、BCSFE処理を実行しない。Cloud Tasksの再配送で複数Jobが起動しても、DB claimできるexecutionは1つだけにする。
- Job起動APIがタイムアウトした場合は「起動していない」と決めつけない。Cloud Run Job execution一覧とexecution IDを照合し、同じexecution_idをclaimできるJobだけが処理を開始する。

### 15.5 Firestore操作量の概算（仮置き）

注文1件あたりの初期目標値:
- 書き込み: 25〜40 document writes（注文・決済attempt・状態イベント・execution・outbox・slot/lock・進捗chunkを含む）。
- 読み取り: 30〜60 document reads（状態検証、transaction、Job claim、進捗確認を含む）。
- 実装では進捗ログをまとめ、ステータスのポーリングを最低5秒間隔程度にし、同じ画面内の短時間readをキャッシュする。
- 50注文/日なら概算で1,250〜2,000 writes/日、1,500〜3,000 reads/日（UI polling・transaction retry増加分を別途加算）。Firestore Standard無料枠の20,000 writes/日・50,000 reads/日より小さい見込み。
- ただし、管理者一覧の全件取得、進捗ポーリング、transaction競合によるretry、複合indexの読み取りは増加要因。実装後にFirestore usage metricsで実測し、上記は保証値として扱わない。
- 50注文/日を超える場合は日次無料枠を超える可能性を再計算する。無料枠は月間ではなく日次適用、Pacific timeでリセットされる。

### 15.6 Cloud Run Jobs compute費用シナリオ（仮置き）

前提: 1 vCPU / 1 GiB memory / 1 task per execution、課金対象実行時間は最低1分。Cloud Run Jobsの無料枠は月240,000 vCPU-secondsと450,000 GiB-seconds（請求先アカウント単位でプロジェクト横断集計）。超過時の通常単価の目安は$0.000018/vCPU-second、$0.000002/GiB-second。東京Tier 1の実請求単価・請求通貨・税はCloud Billingで最終確認する。

| 注文数/月 | 1件の平均実行時間 | vCPU秒/月 | メモリGiB秒/月 | 無料枠適用後のJob compute概算 |
|---:|---:|---:|---:|---:|
| 100 | 10分 | 60,000 | 60,000 | $0（無料枠内） |
| 500 | 5分 | 150,000 | 150,000 | $0（無料枠内） |
| 1,000 | 5分 | 300,000 | 300,000 | 約$1.08（約¥173） |
| 1,000 | 10分 | 600,000 | 600,000 | 約$6.78（約¥1,084） |

換算は2026-11-13のUSD/JPY約159.9円で単純換算した参考値であり、カード/銀行換算、Google請求通貨、税・その他サービス料金は含まない。実処理が2 vCPUを必要とする場合、CPU費用は増える。Cloud Run Jobsは実行中のinstance-based料金で、1分未満の実行にも最低1分が適用される。

**重要:** 1,000件/月・10分/件ではJob compute単体で月1,000円を少し超える可能性がある。Web Service、Firestore、Tasks、Artifact Registry、Logging、外向き通信も含む総額が目標1,000円を超えるケースがあるため、1,000円を保証しない。実際の処理時間をRenderログ・計測で測ってから再試算する。

### 15.7 今回の設計判断

- Firestore Standard Tokyoを主DB候補として継続。
- 進捗ログは別collectionのbounded chunksとし、巨大ドキュメント化を避ける。
- 決済はattemptedを先に確保し、外部受取結果不明時はreview/outcome_unknown。自動再送禁止を維持。
- Jobはinitially maxRetries=0。副作用が安全に再開可能な単位に分離できるまで自動retryを行わない。
- Cloud Tasks outboxでDB更新とTask発行の不整合を回復。
- 4つの共有worker slotsでglobal concurrencyを制限。Job APIの二重起動はexecution claimで排除。
- GitHub runtime-dataは移行元・バックアップとして残し、同時更新DBにはしない。

### 15.8 今回の作業範囲

本節は設計・概算のみ。Firestore/GCPリソース作成、課金有効化、コード変更、データ移行、Render設定変更、PayPay操作、注文受付有効化は実施していない。Cloud Run Jobの最大168時間・無料枠・価格は公式資料を参照したが、実際の処理時間・CPU使用率は未測定。

### 15.9 参照資料

- Firestore transactions: https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions
- Firestore quotas and free tier: https://docs.cloud.google.com/firestore/quotas
- Firestore pricing: https://cloud.google.com/firestore/pricing
- Cloud Run pricing (Services and Jobs): https://cloud.google.com/run/pricing
- Cloud Run Job timeout: https://docs.cloud.google.com/run/docs/configuring/task-timeout
- Cloud Run Jobs create / retries / parallelism: https://docs.cloud.google.com/run/docs/create-jobs
- USD/JPY reference rate (2026-11-13): https://www.boj.or.jp/en/statistics/market/forex/fxdaily/fxlist/fx261113.pdf



## 16. Phase 1 設計レビュー：Firestore / Cloud Tasksの制約反映（2026-11-13）

### 16.1 Firestoreサイズ・TTL

- Firestore documentは最大1MiB - 4 bytes。注文ドキュメントにログ配列やゲームデータ、巨大なAPI応答を入れない。
- transactionの最大request sizeは10MiB。更新対象ドキュメントとindex entryもサイズに含まれるため、暗号文・イベント・ログを無制限に同一transactionへ詰めない。
- Firestore TTL deleteは無料枠に含まれず課金対象。初期実装ではTTL policyを有効にしない。期限切れOTP、lease、ログchunkはアプリ側でexpiryを判定し、低頻度のcleanup処理で件数を制限して削除する。cleanupの実行頻度と削除量も費用試算へ含める。
- Firestore transactionでは全readをwriteより前に実行し、transaction callbackの再実行を前提に副作用を置かない。

### 16.2 Cloud Tasksの重複配信

- Cloud Tasksはat-least-once delivery。非常に稀でも同じtaskが複数回配送されるため、Task名の重複排除だけを安全性の根拠にしない。
- 同じtask名は削除後最大24時間記憶されるが、これも永続的な業務idempotencyではない。Firestoreのexecution claimと状態遷移を最終的な重複防止境界にする。
- Cloud Tasks HTTP targetはDispatcherが2xxを返すまでタスクを再試行し得る。HTTP targetの通常deadlineは10分、最大30分。DispatcherはJob起動要求と起動結果の記録だけを行い、BCSFE処理を待たずに短時間で応答する。
- Job起動APIの応答が不明な場合、Dispatcherが盲目的に別executionを作らない。Cloud Run Job executionの状態照合とexecution_id claimを行い、重複起動しても未claimのJobだけが安全に終了する設計にする。

### 16.3 追加した実装受入条件

1. Firestoreのorders documentに平文のtransfer code、PIN、PayPay password、access token、session cookieが一切存在しない。
2. PayPay受取前にpayment_attempts/{digest}をattemptedで確保し、同じdigestは2回目以降claim不可。
3. PayPay acceptのタイムアウト、アプリ停止、DB確定失敗の各ケースでreview/outcome_unknownへ進み、自動再受取が起きない。
4. Cloud Tasks同一taskを2回配送しても、実行権限を取得できるJobは1つだけ。
5. Jobが処理中に強制終了された場合、再起動したJobが同じ注文を自動再実行しない。execution状態をreview/outcome_unknownへ送る。
6. 4枠を超えるJobはBCSFE処理を開始しない。Job実行状態を照合せずに期限切れslotを再利用しない。
7. progress/log更新が毎ログ行のFirestore writeにならない。一定時間・step変更・terminal stateでまとめる。
8. Firestore TTLを初期段階で有効にせず、cleanupは明示的に予算計上する。
9. Firestore transaction callback内から外部APIやCloud Run Jobs APIを呼ばない。
10. 1MiB document limitと10MiB transaction request limitを超える入力・ログをテストで拒否する。

### 16.4 追加参照資料

- Firestore document REST resource / size: https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents
- Firestore quotas (document size / TTL / transactions): https://firebase.google.com/docs/firestore/quotas
- Cloud Tasks delivery and idempotency: https://docs.cloud.google.com/tasks/docs/dual-overview
- Cloud Tasks duplicate execution pitfalls: https://docs.cloud.google.com/tasks/docs/common-pitfalls



## 17. 移行進捗チェックポイント（2026-11-13）

### 完了
- [x] Phase 0: 現行Betaの構成・データ保存・PayPay/Worker状態のコード調査
- [x] Phase 1: 東京リージョン、Cloud Run/Jobs、Cloud Tasks、Firestore、Artifact Registry、Cloud Build、Secret Manager、Loggingの公式仕様・無料枠確認
- [x] Phase 1: Firestoreコレクション案と注文・決済・Jobのtransaction境界を設計
- [x] Phase 1: Cloud Tasks at-least-once配信、Firestore document/transaction制限、TTL料金を設計へ反映
- [x] Phase 1: 1 vCPU / 1 GiBを仮定したJob実行量の参考シナリオを計算
- [x] 各調査・設計結果を本ファイルへ記録し、mainで取得し直して内容を検証

### Phase 1終了時の判断
- アーキテクチャ第一候補はCloud Run Service + Cloud Tasks + Dispatcher + Cloud Run Jobs + Firestore Standard。
- Cloud Run Serviceは東京、request-based billing、min instances 0。
- Firestore Standardは東京、暗号文と業務状態のみ。大きなファイル・ログは保存しない。
- Jobは1 task / execution、parallelism 1、initial max retries 0、全体同時実行4枠をtransactional slotで制御。
- 決済・ゲーム処理は結果不明時に自動再試行せず、review/outcome_unknownへ。
- 小規模利用なら無料枠内に収まる可能性はあるが、1,000件/月・10分/件のJob computeだけで約¥1,084の参考値となり、総額1,000円以内は保証できない。実測値に基づく再試算が必要。

### Phase 2の作業順
1. GitHubの別ブランチに作業用ブランチを作り、Render稼働中のmainへ直接不完全な変更を入れない。
2. 現行SQLite/Runtime Storeの呼び出し境界を一覧化し、Shop / Payment / Execution / Auth のRepository interfaceを設計する。
3. Firestore clientを遅延初期化するadapterを作成し、ローカル単体テストではfake repositoryを使えるようにする。
4. 注文状態遷移とpayment attemptのtransactionを先に実装し、重複・競合・DB障害のテストを追加する。
5. outbox / execution claim / slot lease / transfer lockを順に実装する。外部APIはfakeで検証し、PayPay実リクエストは送らない。
6. 既存のRender向けSQLite backendを残し、環境変数でbackendを明示選択。Cloud Run設定が未完成の間はRender動作を変えない。
7. Firestore Emulator等を利用できるテスト環境を整え、transaction retry・重複配送・worker crash・outcome_unknownを検証する。
8. 移行コードが一通り揃った後に、コードレビュー・テスト・Cloud Run専用のisolated staging準備へ進む。

### Phase 2開始前の安全条件
- 現在のCloud Run/GCP project、billing account、IAM、サービスアカウント、Secret Managerは未作成・未確認。
- Cloud Runデプロイ・Firestore作成・課金有効化・PayPay実機テスト・Render切替は、Google Cloud側の準備と別途検証が終わるまで実施しない。
- Phase 2のコード変更は専用ブランチで行い、main/Renderへ自動反映させない。
- SHOP_LIVE_ORDERS_ENABLED=0を維持し、注文受付を有効化しない。
- CatsDaiko5安定版は引き続き変更しない。


## 18. Phase 2開始：コード依存関係・永続化境界の実査（2026-11-13）

### 18.1 作業ブランチ

- feature/cloud-run-firestore-foundation を main の最新コミット af77630aa6bb40dccee88d70ffb2a5e3292f44af から作成した。
- このブランチはCloud Run移行の基盤実装専用。未完成の移行コードを稼働中Renderへ反映しない。
- 安定版 CatsDaiko5、Betaの main、Render設定・デプロイ、runtime-data は変更していない。

### 18.2 現在のモジュール境界と呼び出し箇所

| 領域 | 現在の実装 | Cloud Run移行で必要な変更 |
|---|---|---|
| 公開注文・状態遷移 | shop_store.py | SQLite固有SQLをRepository境界の背後へ移し、Firestoreではトランザクションで同等の状態遷移を実装 |
| PayPay認証・受取台帳 | paypay_gateway.py | 認証アカウント暗号化Blobと決済台帳を分離。決済結果不明時は自動再試行しない |
| GitHubランタイム保存 | github_runtime_store.py | Render用互換バックエンドとして残す。GitHub Contents APIのスナップショットを複数Cloud Runインスタンスの主DBにしない |
| Webルート・処理起動 | app.py | HTTPルートから注文受付・Dispatcher・Job起動を分離。BCSFE処理をWebリクエスト内で実行しない |
| BCSFEワーカー | app.py 内の _worker_entry / _run_job / _launch_job | Job単位の実行エントリポイントに分離し、進捗・結果・実行ロックを永続化 |
| 管理者認証 | github_auth_store.py / auth_store.py | GitHub認証ストアを維持するか、永続DBへ移すかを明示選択。一時ディスクを永続保存先にしない |
| 依存関係 | requirements.txt | Firestore/Cloud Tasksクライアントは必要な段階で追加し、既存Render環境へ不用意な変更を入れない |

### 18.3 実査で確認した重要な永続化リスク

1. app.py の _jobs、_pending_jobs、_slots、_active_transfers はプロセス内の辞書・deque・list・setである。Webプロセス再起動やCloud Runの複数インスタンス化で共有されず、待機中のジョブ・進捗・実行枠・重複防止情報を失う。
2. 現在のワーカーは multiprocessing で起動され、進捗はPipe経由で親プロセスの _jobs に反映される。Cloud RunではWebコンテナ内の子プロセス管理をそのまま移植せず、Job実行プロセスからFirestoreへ進捗を保存する必要がある。
3. 現在の実行枠上限4はプロセス内 _slots による制御であり、複数Job/複数インスタンスを横断する排他ではない。Firestore上の4個のslot leaseをトランザクションで取得する設計が必要。
4. 同一引継ぎコードの重複防止に使う _active_transfers と fcntl/ファイルロックは、コンテナをまたぐ排他として使えない。Firestore上のHMAC digestロックへ置き換える。
5. paypay_gateway.py の _pending_otp はプロセス内メモリである。Cloud Runのスケールダウン・再起動後にSMS認証の途中状態が失われるため、必要なら短寿命・暗号化・単回利用の永続状態へ変更する。OTP自体は保存・ログ出力しない。
6. github_runtime_store.sqlite_database はSQLite全体を暗号化スナップショットとしてGitHubへ書き戻し、ロックはプロセス内である。複数Cloud Run writerによる同時更新ではLost Update/競合が起こり得るため、Cloud Runの主DBに流用しない。
7. shop_store.py の注文状態・価格・暗号化済み資格情報・PayPay決済digest・job_idは同じSQLite行に保存されている。Firestore移行では注文本体、決済試行、実行状態、イベント履歴を分離し、状態遷移の原子性を明示する。
8. 管理者注文一覧は現在 shop_store.admin_orders() が保存済み引継ぎ情報を復号して返す。Firestore移行後も管理者権限チェックを維持し、公開注文API・ログ・イベント・Task payloadへ資格情報を混入させない。

### 18.4 Webルートと決済・実行の現在の流れ

- 注文作成: shop_create_operation_order → shop_store.create_operation_order。
- 注文表示・状態確認: shop_order_page / shop_order_status → shop_store.public_order。
- 引継ぎ情報送信: shop_submit_credentials → save_credentials → claim_execution → 現行ワーカー起動 → bind_job → fulfilling。
- PayPay受取: shop_paypay_submit → claim_payment → paypay_gateway.receive_payment → complete_payment。外部決済API呼び出しとDB状態確定は単一トランザクションにできないため、結果不明時の review/outcome_unknown を厳守する。
- 管理者操作: admin_paypay_* と admin_orders_*。現在OTP待機の有無は paypay_gateway._pending_otp を直接参照しているため、将来はGatewayの公開状態APIへ置き換える。
- 現在の注文受付ガード SHOP_LIVE_ORDERS_ENABLED=0 は維持する。

### 18.5 Phase 2の実装順序（更新）

1. 注文・決済・実行・進捗のRepository契約を定義し、WebルートがSQLite接続・SQLへ直接依存しない境界を作る。
2. 既存SQLite実装をRender互換アダプターとして保持し、既存テストで振る舞いを固定する。
3. Firestoreアダプターを追加する。Firestoreのトランザクション・条件付き更新・一意性制約を模擬またはEmulatorでテストし、未設定時に本番コードが誤ってSQLiteへフォールバックしないようにする。
4. 決済claim、注文作成、資格情報保存、実行claim、slot lease、transfer lock、outboxを個別のトランザクション単位で実装する。
5. Cloud Tasks DispatcherとCloud Run Job entrypointを追加する。Task payload/Job argsは注文ID・実行IDのみとし、Job再試行は初期 maxRetries=0。
6. Webアプリの注文・進捗参照をFirestore Repository経由へ切り替え可能にする。バックエンド選択は明示設定のみとし、暗黙の自動切替を禁止する。
7. PayPay実通信・実注文を使わず、モックとFirestore Emulatorで重複・競合・タイムアウト・不確定結果を試験する。

### 18.6 このチェックポイントでの変更・未変更

実施:
- 専用作業ブランチを作成。
- Betaの主要ストレージ・PayPay・ワーカー・管理者認証モジュールとWeb呼び出し箇所を読み、移行依存関係を記録。

未実施:
- アプリケーションコード変更、Firestore/Cloud Tasksクライアント追加。
- GCPプロジェクト・課金・IAM・Secret Manager・Firestore・Cloud Tasks・Cloud Runリソース作成。
- Render設定・デプロイ、runtime-data、PayPay認証・決済データの変更。
- PayPay実ログイン・実決済・実注文。
- SHOP_LIVE_ORDERS_ENABLED の有効化。

次の作業は、Repository契約とSQLite互換アダプターの設計・実装。Firestore接続情報がない状態でも既存SQLiteテストを維持できる形で進める。
## 19. Phase 2実装チェックポイント：保存先選択と決済Repository抽出（2026-11-13）

### 19.1 実装内容

- shop_storage.py を追加し、注文DBの保存先選択を一箇所に集約した。
- SHOP_STORAGE_BACKEND の既定値は legacy。現在のSQLite + 任意のGitHub暗号化スナップショット経由を維持する。Render側に新しい環境変数を追加しなくても従来動作を保つ。
- SHOP_STORAGE_BACKEND=firestore はFirestoreアダプターが完成するまで明示的にエラーにし、SQLiteやGitHub保存先へ黙ってフォールバックしない。
- 未知のバックエンド値もエラーにする。GitHub Runtime Storeの例外はストレージ境界で ShopStorageError に統一する。
- shop_payment_repository.py を追加し、PaymentRepository Protocolと現行SQLの SQLitePaymentRepository を分離した。
- claim_payment / release_payment / complete_payment は shop_store.py からRepositoryへ委譲する。既存の条件付きUPDATE、BEGIN IMMEDIATE、決済後状態確定エラーを維持する。
- test_shop_store.py にFirestore未実装時のfail-closedと未知バックエンド拒否テストを追加し、テストごとにlegacy backendを明示する。

### 19.2 CI検証

- Draft PR #9: https://github.com/tenma2066-tech/CatsDaiko5-Beta/pull/9
- GitHub Actions CI run 43429994230（初回ストレージ境界実装）: success。
- GitHub Actions CI run 43430175204（決済Repository抽出を含む最新コード）: success。
- 最新runでは依存関係インストール、BCSFE API互換性、Application self-test、Save audit/repair、Parallel、Improvement、Character customization、GitHub auth/runtime store、PayPay gateway、Shop store tests がすべて成功。
- CIはGitHub Actions上で実行済み。Cloud Run/Firestore Emulatorでの統合テストはまだ未実施。

### 19.3 現在のGitHub状態

- 作業ブランチ: feature/cloud-run-firestore-foundation。
- PR #9はDraft / open / not merged。mainへは未反映。
- 最新ブランチHEAD: 0697751e4ab816a87ce33b86dcd9761e33c5da28。
- 変更対象は shop_storage.py、shop_payment_repository.py、shop_store.py、test_shop_store.py、本計画書。

### 19.4 未実施・安全状態

- Firestore adapter、注文全体のRepository抽出、Cloud Tasks outbox、実行claim/slot lease/transfer lock、Job entrypointは未実装。
- GCP project/billing/IAM/Secret Manager/Firestore/Cloud Tasks/Cloud Runは未作成。
- Render設定・デプロイ・runtime-data・PayPay認証/決済データは変更していない。
- PayPay実ログイン・実決済・実注文は実施していない。
- SHOP_LIVE_ORDERS_ENABLED=0 は維持。CatsDaiko5安定版とBeta mainは変更していない。

次の作業は、注文作成・資格情報保存・実行claimを順番にRepositoryへ抽出し、Firestore transaction adapterに必要な原子操作を明確化する。SQLite互換テストを継続し、Cloud Run用バックエンドが未完成の間はlegacy以外を拒否する。

## 20. Phase 2実装チェックポイント：注文Repositoryの抽出（2026-11-13）

### 20.1 今回の実装

- shop_order_repository.py を追加し、OrderRepository契約とSQLiteOrderRepositoryを実装した。
- 注文作成（通常商品・代行操作注文）、注文読み出し、管理者一覧取得をRepositoryへ移した。
- 引継ぎ情報の保存をRepositoryへ移した。アクセスtokenのSHA-256 hash照合と、paid / credentials_submitted=0 / job_id IS NULL の条件付き更新を一つのDB更新で実施する。
- 実行claimをRepositoryへ移した。access hash、paid、credentials_submitted=1、job_id IS NULLを一つの条件付きUPDATEで検査し、重複実行を拒否する。
- Job IDのbindとstarting状態のreleaseをRepositoryへ移した。
- 注文状態遷移（awaiting_payment / payment_processing / paid / review / fulfilling / completed / cancelled）の検証と条件付き更新をSQLiteOrderRepository内のトランザクションへ移した。
- shop_store.py は入力検証、暗号化、公開APIの返却形式を維持しつつ、注文データのDB操作をRepository経由へ変更した。
- 通常商品注文の service_type は既存仕様どおり other-service を維持する。既存の価格計算・暗号化・注文ID/アクセスtoken生成は変更していない。

### 20.2 変更ファイル

- shop_order_repository.py（新規）
- shop_store.py（注文作成・読み書き・資格情報保存・実行claim・状態遷移のRepository委譲）
- docs/CLOUD_RUN_MIGRATION_PLAN.md（本チェックポイント）

### 20.3 検証状況

- PR #9 の最新コミットはGitHub Actions CIで検証中。最新run ID: 43430730247。
- 直前の決済Repository抽出時点のCI run 43430175204 は success。
- 注文Repository抽出後のCI結果はまだ未確定。成功を確認するまでは統合済み・動作確認済みとは扱わない。
- Firestore Emulator/Cloud Run統合テストは未実施。

### 20.4 安全状態と残作業

- 専用ブランチ feature/cloud-run-firestore-foundation のみを変更。PR #9 はDraftのまま、mainへ未マージ。
- Firestore adapter、Cloud Tasks outbox、Firestore上の実行claim/slot lease/transfer lock、Job entrypointは未実装。
- GCP project/billing/IAM/Secret Manager/Firestore/Cloud Tasks/Cloud Runは未作成。
- Render、runtime-data、PayPay認証・決済データは変更していない。PayPay実ログイン・実決済・実注文も未実施。
- SHOP_LIVE_ORDERS_ENABLED=0 を維持。安定版 CatsDaiko5 と Beta main は変更していない。

次の作業はCI結果を確認し、失敗があれば修正する。その後、管理者注文読み出し・注文状態の完全なRepository境界を確認し、Firestore側のtransaction contractとEmulatorテスト設計へ進む。

## 21. OrderRepository契約の仕上げ・検証状況（2026-11-13）

- OrderRepository Protocolに、注文作成、注文取得、一覧、資格情報保存、実行claim、Job bind/release、注文状態遷移の全操作を明記した。
- SQLiteOrderRepositoryは上記契約を実装し、shop_store.pyの注文DBアクセス（スキーマ初期化を除く）をRepository経由へ集約した。
- 通常注文の service_type が従来どおり other-service になる回帰テストを追加。
- 誤ったアクセスtokenでは引継ぎ情報保存・実行claimができず、正しいtokenなら成功するテストを追加。
- 注文Repository抽出時点のGitHub Actions run 43430778059（commit b6afa9be86fe70ed15e77cb6f9f118b632337ead）は全ジョブ・全ステップ成功。Shop store testsも成功。
- その成功run以降にOrderRepository Protocolと認証/互換性テストを追加したため、最新HEAD（7b453685a7c2e427f32c983f4d2d30a5e6bf6832）でのCIは別途確認が必要。最新CIが成功するまで最終検証済みとはしない。
- 変更は引き続き feature/cloud-run-firestore-foundation とDraft PR #9のみ。mainへ未マージ。
- Firestore adapter/Emulator、Cloud Tasks outbox、Job dispatcher/entrypoint、slot lease/transfer lockは未実装。GCP/Render/PayPay/実注文は未変更。SHOP_LIVE_ORDERS_ENABLED=0を維持。

次の作業は最新CIを確認し、必要な修正を行う。次段階ではFirestore adapterに必要なトランザクション契約を定義し、Firestore Emulatorを用いた並行claim・重複Task・不確定決済結果の試験を追加する。

## 22. Firestore Repository adapterの初期実装（2026-11-13）

### 22.1 追加したもの

- shop_firestore_repository.py を追加し、FirestoreOrderRepositoryとFirestorePaymentRepositoryの初期実装を作成した。
- FirestoreOrderRepositoryは注文作成、取得、一覧、引継ぎ情報の暗号化Blob保存、実行claim、Job bind/release、注文状態遷移を実装。状態変更はFirestore transaction内で読み取り・条件確認・更新する。
- FirestorePaymentRepositoryは決済claim/releaseと、決済完了時のpayment_attempts digest一意化・注文paid更新をtransactionで実施する。
- requirements.txt に google-cloud-firestore>=2.16 を追加。
- test_firestore_repository.py を追加。FIRESTORE_EMULATOR_HOSTがある場合、決済・資格情報保存・同時実行claim・状態遷移・重複payment digestをFirestore Emulatorで検証する。Emulatorがない通常CIでは明示的にskipする。
- .github/workflows/ci.yml にFirestore repository test commandを追加。通常CIでは依存パッケージのimportとskip判定まで確認する。
- Firestore adapterはまだshop_storage / shop_storeの実行時選択へ接続していない。SHOP_STORAGE_BACKEND=firestoreは引き続きfail-closed。PayPay ledgerの全状態移行、OTP state、outbox、worker slot lease、transfer lockが未完了のためである。

### 22.2 注意点・未検証事項

- Firestore Emulatorを起動した統合テストはまだ実行していない。Emulator環境での実トランザクション検証を成功扱いしない。
- FirestorePaymentRepositoryのpayment_attemptsは初期のdigest重複防止・完了記録であり、既存 paypay-ledger.sqlite3 の attempted / pending / review / outcome_unknown ライフサイクル全体を置き換えたものではない。
- Firestoreへの本番データ移行、Secret Managerへの暗号化鍵移設、Cloud Tasks/Cloud Run Job連携、Firestore Security Rules/IAM、費用実測は未実施。
- Google Cloud credentials/project/billingは不要なまま。認証情報をコードやGitHubへ追加していない。

### 22.3 安全状態

- 専用ブランチ feature/cloud-run-firestore-foundation とDraft PR #9のみ変更。mainへ未マージ。
- Render設定・デプロイ・runtime-data・PayPay認証/決済データは変更なし。実ログイン・実決済・実注文なし。
- SHOP_LIVE_ORDERS_ENABLED=0を維持。安定版CatsDaiko5とBeta mainは未変更。

次の作業は最新CIを確認し、Firestore adapterのEmulator実行環境を用意するか、テスト可能な専用CIジョブを設ける。その後、既存PayPay ledger/OTP状態を含む決済Repositoryの完全な移行境界を設計し、Cloud Tasks outbox・slot lease・transfer lockを実装する。

## 23. Firestore adapter初期実装のCI結果（2026-11-13）

- 最新コード・計画書を含むPR #9 HEAD `c544f0e6aa76a528c0603b5bc26446dc8e3f622e` のGitHub Actions run `43431398137` は success。
- 依存関係インストール、BCSFE API互換性、Application self-test、Save audit/repair、Parallel、Improvement、Character customization、GitHub auth/runtime store、PayPay gateway、Shop store、Firestore repository test commandの全ステップが成功。
- Firestore repository test command自体は成功したが、FIRESTORE_EMULATOR_HOST未設定のためEmulator統合ケースはskipされている。Firestore transactionの実接続検証を完了した意味ではない。
- 最新作業ブランチHEADは本チェックポイント更新前のコードHEAD c544f0e6aa76a528c0603b5bc26446dc8e3f622e。追加した本章はdocsのみ。
- PR #9はDraft/open/not merged。main、Render、GCP、runtime-data、PayPay設定、実注文に変更なし。SHOP_LIVE_ORDERS_ENABLED=0を維持。

次の実装はFirestore Emulatorを利用した実トランザクション試験環境の整備と、PayPay台帳（attempted / pending / review / outcome_unknown）・OTP状態を含む永続化契約の完成。その後Cloud Tasks outbox、global worker slot lease、transfer lockを追加する。Firestore backendはこれらと本番切替手順が完成するまで有効化しない。

## 24. Cloud Tasks / Cloud Run Workerの実装（2026-11-13）

### 24.1 Dispatcherとoutbox

- shop_cloud_run_dispatcher.py を追加。Cloud Tasks task bodyは order_id と execution_id のみ。PayPayリンク、引継ぎコード、PIN、Cookie、OTP、暗号化blobは入れない。
- Task名はexecution IDから決定的に生成。Cloud Tasks createがAlreadyExistsの場合は再送成功として扱い、Firestore outboxを task_enqueued に進める。
- task_outbox は注文とexecutionの作成と同じFirestore transactionで作る。publish途中のプロセス終了に備え、publish_pending による回復処理を追加。
- shop_cloud_run_dispatcher_service.py を追加。非公開Cloud Run Serviceとして配置し、Cloud Run IAMでCloud Tasks OIDC service accountからの呼び出しだけを許可する。空きslotがない場合はHTTP 503でCloud Tasks retryを要求。
- /sweep endpointを追加。Cloud Schedulerから定期実行し、未publish outboxを再送する。

### 24.2 グローバル実行slotと重複防止

- DispatcherがJob起動前にFirestore transactionで4つの worker_slots から1枠を予約する。空き枠がなければoutbox状態を変更せず、Taskをretryさせる。
- Workerは予約済みslotとexecution IDを照合してからclaimする。4枠を超えるJobが同時に実処理へ進まない設計。
- 引継ぎコードの重複ロックは専用pepperを使うHMAC-SHA256。平文・単純SHA256をFirestoreへ保存しない。
- Job起動APIの結果が不確定な場合はoutbox/execution/orderをreviewへ移し、slot予約を自動解放しない。ゲーム側結果が不確定な場合はtransfer lockも保持する。
- Workerが起動前の検証で失敗した場合に限り、自分の予約slotを安全に解放する。
- キュー待機中の注文はJob起動前にのみキャンセル可能。起動開始後はキャンセルを受け付けない。

### 24.3 Cloud Run Job Worker

- shop_cloud_run_worker.py を追加。Jobは注文ID・execution ID・slot IDだけを引数に受け取り、Firestoreから暗号化済み注文データを取得する。
- 既存のBCSFE処理 app._worker_entry / _run_job を再利用し、Webプロセス内のin-memory queueに依存せず1注文を1 Jobで処理する。
- 引継ぎコード・PINはJob内でのみ復号。進捗ログからtransfer code / PIN / access hashをredactする。
- 処理結果（新しい引継ぎコード等）はPAYPAY_ENCRYPTION_KEYで暗号化してFirestoreへ保存。平文resultは保存しない。
- Firestoreへのprogress保存に失敗した場合、成功扱い・slot解放をしない。再実行で二重操作が起きないようreview/保留を優先する。

### 24.4 Firestore注文・PayPay状態のアプリ接続

- SHOP_STORAGE_BACKEND=firestore を明示選択できるようにし、Firestore選択時にSQLiteへ自動fallbackしない。
- Webの注文作成、引継ぎ情報保存、注文一覧、注文進捗、待機中キャンセルをFirestoreへ接続。
- PayPayリンクdigestを受取処理の前にFirestore transactionでclaim。既存GitHub runtime-dataの旧PayPay ledgerは読み取り専用の重複チェックとして残し、新しいattemptはFirestoreへ記録する。
- PayPay attempt状態はattempted / pending / paid / review / outcome_unknown / rejectedをtransactionで管理。曖昧な受取結果をawaiting_paymentへ自動で戻さない。
- 管理者SMS OTP stateはPAYPAY_ENCRYPTION_KEYで暗号化してFirestoreへ短時間保存し、Cloud Runのscale-to-zero後も継続できるようにした。
- Web Serviceは初期max instances 1を推奨。GitHub runtime-data上のPayPay account / 旧ledgerのwriter競合を避けるため。主DBはFirestore。

### 24.5 コンテナと構築手順

- Dockerfile と .dockerignore を追加。.env、runtime data、SQLite、ログ、暗号化blob、鍵ファイルをimage build contextから除外する。
- firebase.json / firestore.rules を追加し、GitHub Actions上でFirestore Emulatorを起動する構成にした。
- docs/CLOUD_RUN_DEPLOYMENT_RUNBOOK.md を追加。東京region、Artifact Registry、Firestore、Cloud Tasks、サービスアカウントIAM、Secret Manager、Cloud Run Web/Dispatcher/Job、Cloud Schedulerの構築順を記載。
- Cloud Run Job commandはJob定義時に python shop_cloud_run_worker.py と設定し、実行ごとのoverrideはargsだけにする。Cloud Run ContainerOverrideはcommand overrideをサポートしないため。

## 25. 最新の検証・未完了ゲート（2026-11-13）

- 既存CI run 43431398137 はsuccess。
- Cloud Tasks / Worker / Webの追加テストを含む最新CIはrun 43434924529。Python依存関係、既存回帰テスト、PayPay gateway、Shop store、Cloud Run dispatcher、Cloud Run worker testsまでsuccess。Firestore Emulator起動・transaction integration tests・Docker buildはこのrunの完了待ち。
- Firestore Emulator testsは注文/決済状態、同時claim、payment digest重複、outbox重複配送、4枠予約、transfer lock保持、OTP暗号化状態、shop_store Firestore接続を検証する。
- CIのFirestore Emulatorは本番GCP接続ではない。本番Firestore/Cloud Runの実トランザクションとIAMは未検証。
- GitHub runtime-dataの既存注文DB/旧PayPay ledgerの完全なFirestore移行・照合ツールは未完成。PayPay account.encは引き続きGitHub runtime-dataを使用する。
- Workerの異常終了後にCloud Run Execution状態を照合するreconciler/管理者解除フローは未完成。unknown outcome時のslot/transfer lockは自動解放しないため安全側だが、手動復旧が必要。
- GCP Project/Billing/API/Firestore/Cloud Tasks/Cloud Run/Secret Managerは未作成。Renderも未変更。
- SHOP_LIVE_ORDERS_ENABLED=0 を維持。安定版CatsDaiko5とBeta mainは未変更。

次の実装は最新CIのEmulator transaction結果を確認し、失敗を修正する。その後、Cloud Run Executionのreconciliation、既存runtime-dataの読み取り専用export/import照合、Firestoreのadmin review解決手順を実装する。これらが終わって初めてGCP Project/Billing設定をユーザーに依頼する。

## 24. Cloud Run実行基盤の初期配線・原子性レビュー（2026-11-13）

### 24.1 追加済み実装（ブランチ上）

- Firestore Workflow Repositoryにtransactional outbox、Cloud Tasks配送状態、4枠のglobal worker slot予約、HMAC transfer lock、実行claim/進捗/終了、短時間暗号化OTP stateの初期実装を追加。
- Cloud Tasks publisherはexecution IDから決定的なTask名を作り、payloadにはorder_id / execution_idだけを含める。AlreadyExistsは配送再試行として扱う。
- Private Dispatcher ServiceとCloud Run Job launcher / Worker entrypointを追加。Job引数はorder ID・execution ID・slot IDのみ。
- WorkerはFirestoreから暗号化済み注文情報を読み、既存のBCSFE worker entrypointを再利用する。ゲーム処理開始後の例外は結果不明としてreviewにし、transfer lockを保持する。
- Firestore backend選択時のPayPay決済attempt stateとOTP stateをFirestoreへ置く初期経路を追加。PayPayアカウント本体と既存SQLite ledgerはまだ移行していない。
- Cloud Run用Dockerfile、.dockerignore、Firestore rules、CI Emulator起動、Cloud Run Deployment Runbookを追加。
- RunbookはTokyo region、min instances 0、Web max instances 1、Worker maxRetries 0、Cloud Tasks 4 concurrent dispatch、Scheduler outbox sweepを前提にする。

### 24.2 今回見つけて修正した原子性の問題

- 以前のFirestore注文画面は、引継ぎ情報保存とexecution/outbox作成を別々のtransactionで実行していた。
- その間にWeb processが停止すると、credentials_submittedだけがtrueになり、execution/outboxが存在しない孤立注文になる可能性があった。
- FirestoreWorkflowRepository.submit_credentials_and_enqueue() を追加し、暗号化引継ぎ情報、credentials_submitted、active_execution_id、job_id=queued、execution document、task_outbox documentを単一Firestore transactionで作成するよう変更。
- Firestore注文画面は新しい shop_store.submit_credentials_and_enqueue() を呼び、従来の2段階更新を使わない。
- Firestore Emulator統合テスト test_credentials_and_outbox_are_created_atomically を追加し、成功時の注文・execution・outbox整合性と二重送信拒否を検証する。

### 24.3 現在のCI状況

- 直近の既存コード検証ではCloud Run Dispatcher tests、Worker tests、Shop store testsまで成功。
- Firestore Emulatorを含むCI run 43434924529 は進行中だった（Firebase CLI導入・Emulator test前）。
- 今回の原子性修正後、GitHub Actions run 43435141360（parallel regression）は進行中。修正後のCI結果は完了を確認するまで成功扱いしない。
- Emulator統合テストはCIで実行する設定を追加済みだが、最新runの最終結果待ち。

### 24.4 残っている重要な移行ゲート

- PayPay payments.sqlite3 のattempted / pending / paid / review / outcome_unknown / rejected lifecycle全体、重複リンク履歴の移行と照合。
- PayPay account.enc のGitHub runtime-data保管をSecret/Firestore等へ移すか、単一writer制約を維持して安全に継続するかの最終設計。
- OTP状態のconsumeを実際のSMS認証処理フローに接続し、同時要求・再送・期限切れのテストを追加。
- Workerの進捗・結果・監査レポートをFirestore document上限内で保持する設計。現在の配列ログ保存は上限到達リスクがあるため、bounded chunksへの移行が必要。
- Dispatcherの起動APIタイムアウト時、Cloud Run Job実行一覧との照合・回復手順を追加する。結果不明時にJobを再起動しない安全方針は維持。
- Cloud Tasks task retry exhausted / outbox task_enqueued滞留の回復、reserved slotのreaper、Cloud Run実行状態を照合したslot解放を実装する。lease期限だけでslotを再利用しない。
- Firestore Security Rules / IAMの最小権限レビュー、データ移行ツール、旧データとの照合、Cloud Run実機・費用検証。
- Firestore backendはPayPay ledger移行、OTP consume、outbox回復、Workerログ分割、Emulator試験が完了するまで本番切替しない。

### 24.5 安全状態と次の作業

- 変更先は引き続き feature/cloud-run-firestore-foundation / Draft PR #9のみ。mainへ未マージ。
- CatsDaiko5安定版、Beta main、Render設定・デプロイ、GitHub runtime-dataは変更していない。
- GCP project/billing/IAM/Secret Manager/Firestore/Cloud Tasks/Cloud Runリソースは未作成。
- PayPay実ログイン・実決済・実注文は未実施。SHOP_LIVE_ORDERS_ENABLED=0 を維持。
- PAYPAY_ENCRYPTION_KEY は変更していない。
- 次は今回の最新CIとFirestore Emulator結果を確認し、失敗修正後にPayPay ledgerの完全なRepository化、OTP single-use接続、bounded execution log chunks、outbox/slot recoveryへ進む。


## 25. Worker終了後の回復設計をRunbookへ反映（2026-11-13）

- Dispatcherに追加された /reconcile エンドポイントを運用手順へ反映。
- Cloud Schedulerから1分ごとに非公開Dispatcherの /reconcile をOIDC認証で呼び出す手順を docs/CLOUD_RUN_DEPLOYMENT_RUNBOOK.md に追加。
- Cloud Run Job Executionが終了済みなのにFirestoreがrunningのままの場合、成功扱いも自動再実行もせず、reviewへ移行してslotのみ解放する。ゲーム側結果が不明なためtransfer lockは保持する方針を明記。
- DispatcherにはCloud Run Job Execution状態を取得する最小権限が必要。Project全体への広いroles/run.viewer付与は避け、可能な限り権限範囲を絞る。
- Runbook変更commit: 7671540cebfcc57fa5bdb0c0f25750297d7531fc。
- この更新時点で最新PR CIは複数commitに対して再実行中・一部cancelled。Emulator統合テストを含む最新HEADのCI成功は未確認。
- 作業は専用ブランチ / Draft PR #9のみ。main・Render・GCP実リソース・PayPay実アカウント・実注文は変更せず、SHOP_LIVE_ORDERS_ENABLED=0を維持。

次は最新CIを確認し、PayPay旧ledgerのFirestore移行・OTPの並行処理保護・ログdocument分割・outbox task retry exhausted時の回復とslot reaperを実装する。これらが完了するまではFirestore backendを本番で有効化しない。


## 26. PayPay SMS認証状態のsingle-use化（2026-11-13）

- PayPay OAuthのOTP stateをFirestoreへ保存する既存経路を確認。従来のfinish_login()は状態を読み取るだけで、成功後にclearするため、並行した複数リクエストが同じSMS認証stateを同時に利用できる競合があった。
- paypay_gateway.pyに_pending_otp_consume()を追加。Firestore backendではFirestoreWorkflowRepository.consume_otp_session()のtransactionでpending→consumedを原子的に変更し、暗号化stateを一度だけ取得する。legacy backendではプロセスロック下でメモリstateを取り出して消去する。
- finish_login()はOTP形式検証後にconsumeしてからPayPay OAuthを呼ぶ。これにより同一OTP sessionの同時送信・再利用を防ぐ。OTP認証通信がタイムアウト等で結果不明になった場合もstateは再利用せず、管理者は最初からログインを開始する必要がある。安全性優先のトレードオフ。
- test_paypay_gateway.pyにlegacy single-use test、test_firestore_repository.pyにFirestore Emulator single-use testを追加。
- この変更はPayPay実ログイン・SMS送信・決済を実行していない。暗号化キー・保存済みPayPay account.encには触れていない。
- 今回のコードcommit: e40e0cb190e1c51893c6c98051662f2d63840ab4 / 6ff279294f9679ce787bd68c936c1e0d521c67be / ca40a9013b0fb819a976a156984219ec7e12cf9c。
- 次は最新CIでFirestore Emulator testを含む成功を確認する。次の大きな課題はworker snapshotのbounded chunk化とoutbox retry/reaperの回復ロジック。

