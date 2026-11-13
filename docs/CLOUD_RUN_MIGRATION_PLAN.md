# CatsDaiko5-Beta Google Cloud Run 移行計画

- 作成日: 2026-11-13
- 対象リポジトリ: `tenma2066-tech/CatsDaiko5-Beta`
- 対象ブランチ: `main`
- 状態: **計画・調査段階（実装・移行は未着手）**
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

