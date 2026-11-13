# CatsDaiko5-Beta Cloud Run 東京環境 構築手順（Draft）

更新日: 2026-11-13  
対象: `tenma2066-tech/CatsDaiko5-Beta` / `feature/cloud-run-firestore-foundation`  
リージョン: `asia-northeast1`（東京）

> **この手順書は構築準備用。現時点ではGCPリソースを作成していない。** Google CloudのProject/Billing設定、Secret登録、実環境へのデプロイは、ユーザーがGCPアカウントを用意した後に実施する。Renderと安定版CatsDaiko5は維持する。注文受付 `SHOP_LIVE_ORDERS_ENABLED=0` は変更しない。

## 1. 作成するリソース

| リソース | 名前（仮） | 設定 |
|---|---|---|
| Artifact Registry | `catsdaiko5` | Docker / 東京 |
| Firestore | `(default)` | Native mode / 東京 |
| Cloud Tasks Queue | `catsdaiko-orders` | 東京、最大同時dispatch 4 |
| Cloud Run Web | `catsdaiko-beta-web` | 公開、min 0 / max 1 |
| Cloud Run Dispatcher | `catsdaiko-dispatcher` | 非公開、min 0 |
| Cloud Run Job | `catsdaiko-worker` | 1 task、parallelism 1、max retries 0 |
| Cloud Scheduler | `catsdaiko-outbox-sweep` | 1分ごとにoutbox回復処理 |

Web ServiceはGitHub runtime-data上のPayPayアカウント・旧決済台帳との互換性を保つため、初期は最大インスタンス1に制限する。注文・実行・決済状態の主DBはFirestore。Jobは注文ごとに起動し、4つのグローバルslotをFirestore transactionで予約する。

## 2. 事前に必要なもの（ユーザー作業）

1. Google Cloudアカウントで新規Projectを作る（既存Projectを使う場合は専用のBeta用Projectを推奨）。
2. Billingを有効化する。
3. Project IDとProject numberを控える。
4. Cloud Billing予算アラートを月500円・800円・1,000円など複数段階で設定する。予算アラートは自動停止機能ではない。
5. 現行Renderの環境変数名・設定値とGitHub `runtime-data` のバックアップを確認する。値はGitHubやこの手順書へ貼らない。

**重要:** `PAYPAY_ENCRYPTION_KEY` はRenderで使っている現在の値をそのまま移す。再生成・変更すると既存の `paypay/account.enc` 等を復号できなくなる。GitHub PAT、PayPay Cookie、パスワード、OTP、引継ぎコードをソースコードやDockerイメージに入れない。

新規作成する `TRANSFER_LOCK_PEPPER` は32 bytes以上のランダム値をBase64で保存し、以後変更しない。既存の暗号化キーとは別物。

## 3. 有効化するAPI

Cloud ShellでProjectを選択してから実行する。

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID"
```

## 4. データベース・キュー・イメージ保管先

```bash
gcloud artifacts repositories create catsdaiko5 \
  --repository-format=docker \
  --location=asia-northeast1 \
  --project="$PROJECT_ID"

gcloud firestore databases create \
  --location=asia-northeast1 \
  --type=firestore-native \
  --project="$PROJECT_ID"

gcloud tasks queues create catsdaiko-orders \
  --location=asia-northeast1 \
  --max-dispatches-per-second=2 \
  --max-concurrent-dispatches=4 \
  --max-attempts=100 \
  --min-backoff=15s \
  --max-backoff=300s \
  --project="$PROJECT_ID"
```

Firestoreのデータベースは1 Projectにつき既定DBを利用する。Firestore Emulatorでtransaction testが成功してから本番Projectを作る。

## 5. 専用サービスアカウント

用途別に分ける。

- `catsdaiko-web`: Firestore read/write、Cloud Tasks enqueue、必要なSecret read。
- `catsdaiko-dispatcher`: Firestore read/write、Cloud Run Jobをoverride付きで起動、outbox sweep時のCloud Tasks enqueue。
- `catsdaiko-worker`: Firestore read/write、`PAYPAY_ENCRYPTION_KEY` と `TRANSFER_LOCK_PEPPER` のSecret read。
- `catsdaiko-task-invoker`: Cloud Tasksから非公開Dispatcher Serviceを呼ぶOIDC用。
- `catsdaiko-scheduler-invoker`: Cloud Schedulerから非公開Dispatcherの `/sweep` を呼ぶOIDC用。

IAMは必要なリソース単位で付与する。

| Principal | Role / Scope |
|---|---|
| Web SA | `roles/datastore.user`（Project）、`roles/cloudtasks.enqueuer`（Project）、Secret accessor（必要なSecretのみ） |
| Dispatcher SA | `roles/datastore.user`、`roles/run.jobsExecutorWithOverrides`（catsdaiko-worker Job）、`roles/run.viewer`（catsdaiko-worker JobのExecution参照）、`roles/cloudtasks.enqueuer`（Project） |
| Worker SA | `roles/datastore.user`、Secret accessor（暗号化鍵・Pepperのみ） |
| Task Invoker SA | `roles/run.invoker`（catsdaiko-dispatcher Service） |
| Scheduler Invoker SA | `roles/run.invoker`（catsdaiko-dispatcher Service） |

Cloud Tasks用OIDCサービスアカウントはQueueと同じProjectに置く。Web SA（タスク作成者）にはTask Invoker SAへの `roles/iam.serviceAccountUser` を付与する。Cloud Tasks Service Agent（`service-PROJECT_NUMBER@gcp-sa-cloudtasks.iam.gserviceaccount.com`）にもTask Invoker SAへのService Account Userを付与する。Cloud Scheduler用SAにもDispatcher ServiceへのInvokerを付与する。

Cloud Run Jobs起動には `roles/run.jobsExecutorWithOverrides` が必要。通常のInvokerだけではoverride付き起動を許可できない。

## 6. Secret Manager

次の値をSecret Managerへ登録する。既存値は安全な保管元から直接コピーし、チャットやGitHubへ貼らない。

- `SECRET_KEY`: Webセッション用の安定した秘密値。
- `PAYPAY_ENCRYPTION_KEY`: **Renderで使用中の同一値**。
- `TRANSFER_LOCK_PEPPER`: 新規生成したBase64ランダム値（32 bytes以上）。
- `AUTH_GITHUB_TOKEN`: 現行BetaのGitHub認証ストアに必要な最小権限token。
- `RUNTIME_GITHUB_TOKEN`: 現行runtime-data保存に必要な最小権限token。構成上AUTH tokenと共用する場合も権限範囲を確認する。
- `APP_PASSWORD`: 現行Betaで実際に利用している場合のみ。

Cloud Run Web/JobのサービスIDに、必要なSecretだけを読める権限を付ける。値を環境変数の平文やDocker build argsへ書かない。

## 7. コンテナをビルド

このブランチの `Dockerfile` はWeb Serviceを既定起動し、Cloud Run Job / Dispatcherはcommand overrideで個別entrypointを指定する。`.dockerignore` は `.env`、`data/`、`instance/`、SQLite、ログ、暗号化blob、鍵ファイルをbuild contextから除外する。

```bash
gcloud builds submit \
  --tag "asia-northeast1-docker.pkg.dev/$PROJECT_ID/catsdaiko5/app:beta" \
  --project "$PROJECT_ID"
```

## 8. Cloud Run Job（Worker）

Jobは次の条件を必ず守る。

- 1 executionにつき1 task。
- parallelism 1。
- max retries 0（副作用があるため自動再実行禁止）。
- 初期CPU 1 vCPU / memory 1 GiBは仮設定。実機相当の負荷でメモリ・処理時間を計測し調整。
- timeoutは初期1時間。実際の処理時間を測定して必要最小限にする。
- Webと同じイメージを使い、command `python`、args `shop_cloud_run_worker.py` を指定する。
- 環境変数 `GOOGLE_CLOUD_PROJECT` を設定し、`PAYPAY_ENCRYPTION_KEY` と `TRANSFER_LOCK_PEPPER` をSecretから注入する。

WorkerはJob引数として注文ID・実行ID・slot IDだけを受け取る。注文の引継ぎ情報と操作内容はFirestoreから暗号化Blobとして取得し、Job内でのみ復号する。

## 9. 非公開Dispatcher Service

同じイメージを使い、command `python`、args `shop_cloud_run_dispatcher_service.py` で起動する。

環境変数:

- `GOOGLE_CLOUD_PROJECT`
- `CLOUD_RUN_JOB_RESOURCE=projects/PROJECT_ID/locations/asia-northeast1/jobs/catsdaiko-worker`
- `CLOUD_TASKS_PROJECT`
- `CLOUD_TASKS_LOCATION=asia-northeast1`
- `CLOUD_TASKS_QUEUE=catsdaiko-orders`
- `CLOUD_RUN_DISPATCHER_URL`（DispatcherのService URL）
- `CLOUD_TASKS_SERVICE_ACCOUNT`（Task Invoker SAのメール）

Dispatcherは**unauthenticated accessを無効**にする。Cloud Tasksは `/dispatch`、Cloud Schedulerは `/sweep` を呼ぶ。QueueからのOIDCとCloud Run IAMで認証し、アプリ側で公開エンドポイントとして扱わない。

`/dispatch` はFirestore transactionで4枠のうち1枠を予約してからJobを起動する。空きslotがない場合はHTTP 503でCloud Tasksに再試行させる。起動APIの応答が不明な場合は自動再起動せず、注文をreviewにしてslotを保留する。

## 10. Web Service

Web Serviceは公開アクセスを許可するが、注文受付は停止したままにする。

- `SHOP_STORAGE_BACKEND=firestore`
- `SHOP_LIVE_ORDERS_ENABLED=0`
- `GOOGLE_CLOUD_PROJECT` / `FIRESTORE_PROJECT_ID`
- `CLOUD_TASKS_PROJECT`
- `CLOUD_TASKS_LOCATION=asia-northeast1`
- `CLOUD_TASKS_QUEUE=catsdaiko-orders`
- `CLOUD_RUN_DISPATCHER_URL`
- `CLOUD_RUN_DISPATCHER_AUDIENCE`
- `CLOUD_TASKS_SERVICE_ACCOUNT`
- `AUTH_GITHUB_REPO=tenma2066-tech/CatsDaiko5-Beta`
- `RUNTIME_GITHUB_REPO=tenma2066-tech/CatsDaiko5-Beta`
- `RUNTIME_GITHUB_BRANCH=runtime-data`
- `RUNTIME_GITHUB_PREFIX=runtime`
- `PAYPAY_DATA_DIR=/tmp/paypay`
- `PAYPAY_LEDGER_PATH=/tmp/paypay/payments.sqlite3`（移行前ledgerとのread-only重複チェック用）
- `SHOP_DB_PATH=/tmp/shop.sqlite3`（Firestore選択時は使用しない）
- 現行と同じ `SHOP_CATALOG_JSON`（使用している場合）

Webはmin instances 0、max instances 1で開始する。max 1はGitHub runtime-dataを使うPayPayアカウント・旧台帳の単一writer制約を守るため。安定した `SECRET_KEY` をSecret Managerから設定する。

## 11. Outbox回復

Cloud Schedulerから1分ごとに非公開Dispatcherの `POST /sweep` と `POST /reconcile` を呼ぶ。

- `/sweep`: Firestore transactionでoutboxを作成した直後にWebプロセスが終了しても、未publishのTaskを再作成する。タスク名はexecution IDから決定的に生成し、Cloud Tasks側のAlreadyExistsは成功扱いにする。
- `/reconcile`: Cloud Run Job Executionの完了状態を確認し、Firestoreに最終結果が残らなかった実行をreviewへ移す。worker claim前の終了ならslotのみ解放し、claim後の終了ならslotを解放してtransfer lockは保持する。

Scheduler用サービスアカウントにはDispatcher ServiceへのInvokerだけを付与する。Cloud Schedulerには `catsdaiko-outbox-sweep` と `catsdaiko-execution-reconcile` の2ジョブを作成し、どちらも1分ごとに実行する。

## 12. デプロイ後の検証（注文受付は停止したまま）

1. Web `/health` とDispatcher `/healthz` を確認。
2. Web Serviceがmin 0で、アクセスがない時間にインスタンス0へ戻ることを確認。
3. Firestoreのテスト注文を作り、注文・決済attempt・outboxが保存されることを確認。
4. Cloud TasksのOIDC認証でDispatcherが呼ばれることを確認。
5. 4枠を超えるJobが同時にclaimできないことを確認。
6. Jobが進捗・ログをFirestoreへ保存し、Web再起動後も表示できることを確認。
7. 同じTaskを重複配送してもJob起動が1回だけになることを確認。
8. Job起動応答不明・Job強制終了・ゲーム側結果不明時に自動再実行せずreviewになることを確認。
9. OTPがFirestoreへ暗号化保存され、Webのscale-to-zero後もSMS認証を継続できることを確認。
10. Billingの実測値で通常月・集中月の月額見込みを再計算する。

## 13. データ移行・切替の未完了ゲート

- 現行GitHub `runtime-data` の暗号化snapshotから注文DB・PayPay ledgerを読み取り専用で取り出し、Firestoreへ照合/importする専用移行ツールと復旧テストは未完成。
- PayPayアカウント暗号化blobは引き続きGitHub runtime-dataを使用する。新Firestoreの注文台帳とは別管理。
- 旧PayPay ledgerは移行前リンクの重複を防ぐため読み取り専用チェックを残している。
- Firestoreの本番Projectでの実トランザクション、Cloud Run Job実機、費用計測、秘密情報のSecret Manager移設は未実施。
- 実PayPayログイン・受取・実ゲーム処理は、ユーザーの明示的な承認なしに行わない。
- すべての検証が完了するまで `SHOP_LIVE_ORDERS_ENABLED=0` を維持し、Renderは削除・停止しない。

## 14. 予算と安全停止

Cloud Billing予算アラートは通知であり、サービスの自動停止を保証しない。Cloud Run max instances、Job並列数、Cloud Tasks queue rate、Firestore読み書き、Logging量を制限する。月額1,000円を超える見込みが出た場合は実注文を有効化しない。Jobの実行時間・CPU/メモリ・progress write頻度を実測してから費用を再計算する。


## 15. Cloud Run Job終了状態の照合スケジュール

Dispatcherには /reconcile エンドポイントも実装されている。Workerが強制終了し、Firestoreに最終状態を書けなかった場合に、Cloud Run Job Executionの実際の終了状態を照合して注文をreviewへ移す。

Cloud Schedulerに catsdaiko-execution-reconcile を追加し、1分ごとに非公開Dispatcherの POST /reconcile をOIDC認証で呼ぶ。Scheduler用サービスアカウントにはDispatcher Serviceのroles/run.invokerのみを付与する。

この照合は成功扱い・自動再実行を行わない。Cloud Run Executionが終了済みなのにFirestoreがrunningのままの場合は、結果不明としてreviewにし、worker slotのみ解放する。transfer lockは保持し、管理者がゲーム側の状態を確認するまで同じ引継ぎコードを再利用しない。

Schedulerの登録例:

```bash
gcloud scheduler jobs create http catsdaiko-execution-reconcile \\
  --location=asia-northeast1 \\
  --schedule="* * * * *" \\
  --uri="$DISPATCHER_URL/reconcile" \\
  --http-method=POST \\
  --oidc-service-account="$SCHEDULER_SA" \\
  --project="$PROJECT_ID"
```

Cloud Run JobのExecution APIを読むため、Dispatcher Service Accountには対象Job/Projectの実行状態を取得できる最小権限を追加する。roles/run.viewerをProject全体へ付与する前に、可能なら対象リソース単位の権限境界を確認する。

## 15. 公式仕様リンク

- [Cloud Run Job execution / overrides](https://cloud.google.com/run/docs/execute/jobs)
- [Cloud Run IAM roles](https://cloud.google.com/run/docs/reference/iam/roles)
- [Cloud Run container runtime contract（CLOUD_RUN_EXECUTION）](https://cloud.google.com/run/docs/container-contract)
- [Cloud Tasks HTTP target tasks / OIDC](https://cloud.google.com/tasks/docs/create-http-task)
- [Firestore locations](https://cloud.google.com/firestore/docs/locations)
