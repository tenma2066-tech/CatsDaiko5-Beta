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
| Cloud Scheduler | `catsdaiko-outbox-sweep` / `catsdaiko-execution-reconcile` | それぞれ1分ごとにoutbox回復 / Job終了照合 |

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
  --max-attempts=10 \
  --min-backoff=15s \
  --max-backoff=300s \
  --project="$PROJECT_ID"
```

Firestoreのデータベースは1 Projectにつき既定DBを利用する。Firestore Emulatorでtransaction testが成功してから本番Projectを作る。

## 5. 専用サービスアカウント

用途別に分ける。

- `catsdaiko-web`: Firestore read/write、注文登録直後のCloud Tasks enqueue/get_task、Task Invoker SAを指定するためのactAs、必要なSecret read。
- `catsdaiko-dispatcher`: Firestore read/write、Cloud Run Jobをoverride付きで起動、outbox sweep時のCloud Tasks enqueue/get_task、Task Invoker SAを指定するためのactAs。
- `catsdaiko-worker`: Firestore read/write、`PAYPAY_ENCRYPTION_KEY` と `TRANSFER_LOCK_PEPPER` のSecret read。
- `catsdaiko-task-invoker`: Cloud Tasksから非公開Dispatcher Serviceを呼ぶOIDC用。
- `catsdaiko-scheduler-invoker`: Cloud Schedulerから非公開Dispatcherの `/sweep` を呼ぶOIDC用。

IAMは必要なリソース単位で付与する。

| Principal | Role / Scope |
|---|---|
| Web SA | `roles/datastore.user`（Project）、`roles/cloudtasks.enqueuer` + `roles/cloudtasks.viewer`（対象Queue）、`roles/iam.serviceAccountUser`（Task Invoker SAのみ）、Secret accessor（必要なSecretのみ） |
| Dispatcher SA | `roles/datastore.user`、`roles/run.jobsExecutorWithOverrides` + `roles/run.viewer`（catsdaiko-worker Job）、`roles/cloudtasks.enqueuer` + `roles/cloudtasks.viewer`（対象Queue）、`roles/iam.serviceAccountUser`（Task Invoker SAのみ） |
| Worker SA | `roles/datastore.user`、Secret accessor（暗号化鍵・Pepperのみ） |
| Task Invoker SA | `roles/run.servicesInvoker`（catsdaiko-dispatcher Service） |
| Scheduler Invoker SA | `roles/run.servicesInvoker`（catsdaiko-dispatcher Service） |

Cloud Tasks用OIDCサービスアカウントはQueueと同じProjectに置く。Taskを作成するWeb SAとDispatcher SAにはTask Invoker SAへの `roles/iam.serviceAccountUser` を付与する。Cloud Tasks Service Agent（`service-PROJECT_NUMBER@gcp-sa-cloudtasks.iam.gserviceaccount.com`）にもTask Invoker SAへのService Account Userを付与する。Cloud Scheduler用SAにはDispatcher Serviceへの `roles/run.servicesInvoker` を付与する。

Cloud Run Jobs起動には `roles/run.jobsExecutorWithOverrides` が必要。通常のInvokerだけではoverride付き起動を許可できない。

## 6. Secret Manager

次の値をSecret Managerへ登録する。既存値は安全な保管元から直接コピーし、チャットやGitHubへ貼らない。

- `SECRET_KEY`: Webセッション用の安定した秘密値。
- `PAYPAY_ENCRYPTION_KEY`: **Renderで使用中の同一値**。
- `TRANSFER_LOCK_PEPPER`: 新規生成したBase64ランダム値（32 bytes以上）。
- `AUTH_GITHUB_TOKEN`: 現行BetaのGitHub認証ストアに必要な最小権限token。
- `RUNTIME_GITHUB_TOKEN`: 現行runtime-data保存に必要な最小権限token。構成上AUTH tokenと共用する場合も権限範囲を確認する。
- `APP_PASSWORD` は設定しない。公開ショップにサイトアクセスパスワードを要求しない。

Secretの割当は次のとおり。Web Service: `SECRET_KEY`、現行と同じ `PAYPAY_ENCRYPTION_KEY`、`AUTH_GITHUB_TOKEN`、必要なら別個の `RUNTIME_GITHUB_TOKEN`。Worker Job: 同じ `PAYPAY_ENCRYPTION_KEY` と `TRANSFER_LOCK_PEPPER`。Dispatcher Service: Secret不要。値を環境変数の平文やDocker build argsへ書かない。

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
WorkerはCloud Run標準環境変数 `CLOUD_RUN_EXECUTION`（実行名）をownerとして必須にする。この値はCloud Run Jobsが自動注入するため、Job設定で手動定義しない。未設定・不正ならゲーム処理開始前に停止し、予約slotを安全に解放してreviewにする。ランダムownerへのfallbackは行わない。仕様：[Cloud Run container runtime contract](https://cloud.google.com/run/docs/container-contract)。

## 9. 非公開Dispatcher Service

同じイメージを使い、Flaskの開発サーバーではなくGunicornで起動する。Cloud Runのportを `8080`、commandを `gunicorn`、argsを `--workers 1 --threads 4 --bind 0.0.0.0:8080 shop_cloud_run_dispatcher_service:app` にする。Cloud Runのcommand/argsはshellを介さず実行されるため、`$PORT` のshell展開を前提にしない。Dockerfile既定CMDはWeb用のため、Dispatcher Serviceでは明示的にoverrideする。

環境変数:

- `GOOGLE_CLOUD_PROJECT`
- `CLOUD_RUN_JOB_RESOURCE=projects/PROJECT_ID/locations/asia-northeast1/jobs/catsdaiko-worker`
- `CLOUD_TASKS_PROJECT`
- `CLOUD_TASKS_LOCATION=asia-northeast1`
- `CLOUD_TASKS_QUEUE=catsdaiko-orders`
- `CLOUD_RUN_DISPATCHER_URL`（DispatcherのService URL）
- `CLOUD_TASKS_SERVICE_ACCOUNT`（Task Invoker SAのメール）
- `CLOUD_RUN_DISPATCHER_AUDIENCE`（Dispatcher Service URLと同じaudience）
- `CLOUD_RUN_STALE_LAUNCH_SECONDS=600`

Dispatcherは**unauthenticated accessを無効**にする。Cloud Tasksは `/dispatch`、Cloud Schedulerは `/sweep` と `/reconcile` を呼ぶ。Queue / SchedulerのOIDCとCloud Run IAMで認証し、アプリ側で公開エンドポイントとして扱わない。

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
- `AUTH_GITHUB_TOKEN`（Secret Manager。GitHub auth/password storeとruntime-dataの読み書きに必要）
- `RUNTIME_GITHUB_TOKEN`（任意。未設定ならAUTH_GITHUB_TOKENを使用）
- `PAYPAY_ENCRYPTION_KEY`（Secret Manager。現行の暗号化キーと同一値）
- `RUNTIME_GITHUB_REPO=tenma2066-tech/CatsDaiko5-Beta`
- `RUNTIME_GITHUB_BRANCH=runtime-data`
- `RUNTIME_GITHUB_PREFIX=runtime`
- `PAYPAY_DATA_DIR=/tmp/paypay`
- `PAYPAY_LEDGER_PATH=/tmp/paypay/payments.sqlite3`（移行前ledgerとのread-only重複チェック用）
- `SHOP_DB_PATH=/tmp/shop.sqlite3`（Firestore選択時は使用しない）
- 現行と同じ `SHOP_CATALOG_JSON`（使用している場合）

Webはmin instances 0、max instances 1で開始する。max 1はGitHub runtime-dataを使うPayPayアカウント・旧台帳の単一writer制約を守るため。安定した `SECRET_KEY` をSecret Managerから設定する。`APP_PASSWORD` は設定せず、サイトアクセスパスワードを要求しない。

## 11. Outbox回復

Cloud Schedulerから1分ごとに非公開Dispatcherの `POST /sweep` と `POST /reconcile` を呼ぶ。

- `/sweep`: Firestore transactionでoutboxを作成した直後にWebプロセスが終了しても、未publishのTaskを再作成する。outboxの `pending` だけでなく `task_enqueued` も定期確認し、Cloud Tasksのretry上限到達やpurgeでTaskが削除されていれば再登録する。タスク名はexecution IDとFirestoreの `task_generation` から決定的に生成する。
- `/sweep` は期限切れOTP sessionも1回最大100件削除する。OTP状態はFirestoreに暗号化保存し、有効期限15分を過ぎたものは `admin_otp_sessions` からbatch deleteする。Firestore TTL policyは有効化せず、TTL削除料金を避ける。
- Cloud Tasks `create_task` がAlreadyExistsを返した場合は、必ず `get_task` で実Taskの存在を確認する。存在する場合だけtask_enqueuedに進める。削除済みTask名の予約期間中で `get_task` がNotFoundならFirestore generationをtransactionで増やし、新しいTask名で再作成する。get_task自体の通信/権限エラーではoutboxをpendingのまま残す。
- `task_enqueued` のoutboxはCloud Tasks `get_task` で存在確認する。Taskが存在する間は何もしない。Taskが消えて同名予約だけ残っている場合はgenerationをtransactionで増やし、新しいTask名で再作成する。Taskが存在するのに再作成しないため、不要な二重Taskを避ける。
- 1 Task generationのCloud Tasks最大配送回数は10回。generation 0～3の計4世代を上限とし、4世代目のTaskも消失した場合はoutbox / execution / orderを `review` にして自動再登録を停止する。queued状態でworker slotをまだ取得していない場合だけの処理で、launching/runningはこの経路で変更しない。無限再登録による費用増大を防ぐ。
- Cloud Tasksはcreate直後に配送される可能性があるため、Dispatcherはoutboxが `pending` の状態でもtransactional claimを許可する。Task作成後のpublisher更新は、既に `launching` 以降へ進んだ状態を巻き戻さない。
- `/reconcile`: Cloud Run Job Executionの完了状態を確認し、Firestoreに最終結果が残らなかった実行をreviewへ移す。worker claim前の終了ならslotのみ解放し、claim後の終了ならslotを解放してtransfer lockは保持する。

Scheduler用サービスアカウントにはDispatcher Serviceへの `roles/run.servicesInvoker` だけを付与する。Cloud Schedulerには `catsdaiko-outbox-sweep`（POST /sweep）と `catsdaiko-execution-reconcile`（POST /reconcile）の2ジョブを作成し、どちらも1分ごとに実行する。

作成例（同じScheduler SAを両方に使う）。

```bash
gcloud scheduler jobs create http catsdaiko-outbox-sweep \
  --location=asia-northeast1 \
  --schedule="* * * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="$DISPATCHER_URL/sweep" \
  --http-method=POST \
  --oidc-service-account="$SCHEDULER_SA" \
  --oidc-token-audience="$DISPATCHER_URL" \
  --project="$PROJECT_ID"

gcloud scheduler jobs create http catsdaiko-execution-reconcile \
  --location=asia-northeast1 \
  --schedule="* * * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="$DISPATCHER_URL/reconcile" \
  --http-method=POST \
  --oidc-service-account="$SCHEDULER_SA" \
  --oidc-token-audience="$DISPATCHER_URL" \
  --project="$PROJECT_ID"
```

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

## 13. 既存runtime-dataの読み取り専用移行

専用スクリプト `migrate_runtime_data_to_firestore.py` を追加済み。

1. Cloud Shell等でProjectを選択し、FirestoreへのApplication Default Credentialsを用意する。
2. GitHub runtime-dataを読むため、現行の `RUNTIME_GITHUB_REPO` / branch / prefix / tokenを設定する。
3. 現行と同じ `PAYPAY_ENCRYPTION_KEY` をSecret Manager等から安全に環境へ注入する。
4. 最初はdry-runのみ実行する。

```bash
python migrate_runtime_data_to_firestore.py
```

件数・snapshot復号・スキーマ確認ができた場合のみ、バックアップを取得したうえで `--apply` を付けて実行する。

```bash
python migrate_runtime_data_to_firestore.py --apply
```

このツールはGitHub上の暗号化snapshotだけを読み取り、ローカルSQLiteをGitHubへ自動uploadしない。Firestoreへはcreate-onlyで書き込み、既存docを上書きしない。再実行時は `legacy_imported=true` の既存docだけを同じ移行の続きとしてskipする。同じID/digestの既存docが別用途または別注文に属する場合は衝突として停止する。途中で止まった場合は衝突を確認してから同じコマンドを再実行できる。旧job_idが残る注文やpayment_processing中の注文はreviewへ移し、実行を自動再開しない。旧PayPay ledgerのattempted状態はoutcome_unknownとして取り込む。

dry-run summaryでは、注文DBとPayPay ledgerの対応を複数方向から検査する。次のすべてが **0** であることを確認するまで `--apply` は実行できない。

- Order payment digests missing from ledger
- Invalid order payment digests
- Paid orders without a payment digest
- Payment digest/order mismatches
- Paid-order/payment-state mismatches
- Invalid payment ledger rows
- Orphaned payment ledger rows
- Duplicate payment digest records
- Orders with multiple payment attempts

Shop order snapshot schemaとPayPay ledger snapshot schemaの表示が両方 `OK` であることも必須。どちらかが `FAILED` の場合、dry-runは書き込みを行わず、`--apply` は拒否する。金額・数量・時刻・暗号化blob等の型や範囲が不正な注文行、時刻や状態が不正なledger行も部分的なimportを拒否する。

さらに、不正な注文行または不正な決済台帳行が1件でもあれば、部分的なimportを拒否する。決済済み注文は同一注文IDのledger行に対応し、そのattempt stateも `paid` でなければならない。digestの中身や注文IDは表示しないため、旧snapshotを安全な環境で照合してから再実行する。

snapshot取得は `read_blob_readonly` を使い、移行dry-run時にローカルSQLiteをGitHubへ自動upload・初期化しない。PayPayのFirestore決済経路も旧ledgerを読み取り専用で確認し、snapshotが欠落・復号不能・スキーマ不正の場合は決済結果を `review` に固定して自動再受付しない。

PayPay ledger snapshot自体が見つからない場合も `--apply` は拒否される。注文DBだけを移行して過去のPayPayリンク利用履歴を欠落させないため。正しい暗号化ledger snapshotを用意し、すべての照合項目が0であることを確認するまで本番importを行わない。

**本番移行前に、dry-runの件数と管理画面の注文数を照合すること。** 取り込み後もPayPay account.encはGitHub runtime-dataに残す。暗号化キーは変更しない。

## 14. データ移行・切替の未完了ゲート

- 移行ツールは実装済みだが、現行runtime-dataを使ったdry-run件数照合・実データimport・復旧テストは未実施。
- PayPayアカウント暗号化blobは引き続きGitHub runtime-dataを使用する。新Firestoreの注文台帳とは別管理。
- 旧PayPay ledgerは移行前リンクの重複を防ぐため読み取り専用チェックを残している。
- Firestoreの本番Projectでの実トランザクション、Cloud Run Job実機、費用計測、秘密情報のSecret Manager移設は未実施。
- 実PayPayログイン・受取・実ゲーム処理は、ユーザーの明示的な承認なしに行わない。
- すべての検証が完了するまで `SHOP_LIVE_ORDERS_ENABLED=0` を維持し、Renderは削除・停止しない。

## 15. 予算と安全停止

Cloud Billing予算アラートは通知であり、サービスの自動停止を保証しない。Cloud Run max instances、Job並列数、Cloud Tasks queue rate、Firestore読み書き、Logging量を制限する。月額1,000円を超える見込みが出た場合は実注文を有効化しない。Jobの実行時間・CPU/メモリ・progress write頻度を実測してから費用を再計算する。

2026-11-14時点の公式料金表で確認できる主な無料枠・料金上の制約:

| サービス | 無料枠・目安 | 今回の注意点 |
|---|---|---|
| Cloud Run Jobs | 月240,000 vCPU秒 / 450,000 GiB秒（us-central1基準） | 1 vCPU・1 GiBならCPU枠が先に上限。東京リージョン単価と請求先アカウント内の合算使用量を再確認 |
| Cloud Run Services | 月180,000 vCPU秒 / 360,000 GiB秒 / 200万リクエスト（リクエスト課金時） | Web・Dispatcherともmin instances 0を維持 |
| Firestore Standard | 1 GiB保存、50,000 read/日、20,000 write/日、20,000 delete/日、10 GiB送信/月 | 無料枠対象は1 Projectにつき既定DB 1つ。TTL/PITR/Backup等は無料枠外 |
| Cloud Tasks | 月100万課金対象operation | API呼び出しとpush配信試行の両方が対象 |
| Cloud Scheduler | 請求先アカウントあたり月3ジョブ | 今回の2ジョブは、他プロジェクトの利用がなければ無料枠内 |
| Artifact Registry | 請求先アカウントあたり0.5 GiB-month | 古いイメージを残しすぎない。超過分は保存料金が発生 |

無料枠はサービスごとに適用単位が異なり、請求先アカウント単位で他プロジェクトと共有されるものがある。無料枠を使い切っている場合や、東京リージョンの料金が異なる場合は追加料金が発生する。Firestoreの無料枠は日次でリセットされる。**この無料枠表だけでは月額1,000円以内を保証できない。**


## 16. Cloud Run Job終了状態の照合スケジュール

Dispatcherには /reconcile エンドポイントも実装されている。Workerが強制終了し、Firestoreに最終状態を書けなかった場合に、Cloud Run Job Executionの実際の終了状態を照合して注文をreviewへ移す。

Cloud Schedulerに catsdaiko-execution-reconcile を追加し、1分ごとに非公開Dispatcherの POST /reconcile をOIDC認証で呼ぶ。Scheduler用サービスアカウントにはDispatcher Serviceの `roles/run.servicesInvoker` のみを付与する。

この照合は成功扱い・自動再実行を行わない。Cloud Run Executionが終了済みなのにFirestoreがrunningのままの場合は、結果不明としてreviewにし、worker slotのみ解放する。transfer lockは保持し、管理者がゲーム側の状態を確認するまで同じ引継ぎコードを再利用しない。

このScheduler jobはセクション11のScheduler設定で既に作成する。ここで同名jobを重複作成しない。既存jobがある場合は `gcloud scheduler jobs update http` で設定を更新する。

Cloud Run JobのExecution APIを読むため、Dispatcher Service Accountには対象Job/Projectの実行状態を取得できる最小権限を追加する。roles/run.viewerをProject全体へ付与する前に、可能なら対象リソース単位の権限境界を確認する。

Dispatcherがslot予約後、Cloud Run JobのExecution名をFirestoreへ保存する前に停止した場合も、starting状態が永久に残らないようにする。Dispatcher Serviceには環境変数 `CLOUD_RUN_STALE_LAUNCH_SECONDS=600` を設定する（未設定時の既定値も600秒）。Execution名がないstarting状態がこの時間を超えると、/reconcileは注文・executionをreview、outboxをoutcome_unknownに変更する。

**このケースではslotを自動解放しない。** Job起動APIが受理済みか否かを確認できないため、slotを解放して再起動すると同じ注文が二重処理される危険がある。管理者はCloud Run JobのExecutions一覧・該当注文・Firestore execution/outboxを照合し、起動していないことを確認できた場合だけ、専用の安全な復旧手順でslotを解除する。現時点で自動解除機能は未実装。

## 17. 公式仕様リンク

- [Cloud Run Job execution / overrides](https://cloud.google.com/run/docs/execute/jobs)
- [Cloud Run IAM roles](https://cloud.google.com/run/docs/reference/iam/roles)
- [Cloud Run container runtime contract（CLOUD_RUN_EXECUTION）](https://cloud.google.com/run/docs/container-contract)
- [Cloud Tasks HTTP target tasks / OIDC](https://cloud.google.com/tasks/docs/create-http-task)
- [Firestore locations](https://cloud.google.com/firestore/docs/locations)
- [Cloud Run pricing](https://cloud.google.com/run/pricing)
- [Firestore pricing](https://cloud.google.com/firestore/pricing)
- [Cloud Tasks pricing](https://cloud.google.com/tasks/pricing)
- [Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing)
- [Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing)


## 18. Worker snapshot log/report chunk保存

Firestore Execution本体には進捗メタデータのみを保存し、`log` / `warn` / `report` / `skipped` は `execution_logs/{execution_id}/chunks/{chunk_id}` に分離する。

- 1chunkあたり推定128KiB以下、最大32chunk。
- 1つの配列itemが上限を超える、または合計chunk数が32を超える場合は自動切り捨てせず、snapshot保存を明示的に失敗させてreviewへ回す。
- snapshotの更新はtransaction内で旧chunkを削除し、新revisionのchunk群を作成してからExecution revisionを更新する。旧chunkが残り続けない。
- 読み出しは `get_execution()` がindex順にchunkを結合し、従来の配列形式を返す。古いinline配列を持つExecutionも読み出せる。
- Emulatorで大容量logの分割・復元・更新時の旧chunk削除を検証する。

このchunk化はFirestore EmulatorのCI成功を確認してから本番切替候補とする。現時点ではFirestore backendはproductionで有効化しない。


## 19. PayPay ledger snapshotのschema確認

本番migrationの `--apply` 前に、暗号化ledger snapshotの存在だけでなく、SQLite内に `links` テーブルと `digest` / `order_id` / `state` / `created` 列がすべて存在することを確認する。欠落・schema不一致の場合はFirestore書き込み前に停止する。dry-runでの調査は可能だが、空台帳としてimportを続けてはいけない。


## 20. Worker reportのsecret redaction

Workerはlog/warn/errorだけでなく、Firestoreに保存するreport/skipped配列のネストしたdict/listも再帰的にsecret redactionする。転送コード・PINなどを含む文字列を置換し、reportの構造と長さは維持する。単一itemや総chunk数がFirestore chunk上限を超える場合は自動切り捨てせずreviewへ回す。


## 21. Unknown Job launchの読み取り専用調査

Dispatcherの `run_job` 呼び出しがタイムアウトすると、Cloud Run側でJobが受理されたかどうか分からないまま、Firestoreは `review / outcome_unknown` になる。この場合はslotとtransfer lockを保持し、自動再実行しない。

Cloud Shell等で対象branchをcheckoutし、ADCを用意してから読み取り専用の調査コマンドを実行する。必要なIAMは [Cloud Run IAM matrix](CLOUD_RUN_IAM_MATRIX.md) のInspector用read-only権限を参照。

```bash
git clone https://github.com/tenma2066-tech/CatsDaiko5-Beta.git
cd CatsDaiko5-Beta
git checkout feature/cloud-run-firestore-foundation
python -m pip install -r requirements.txt
python shop_cloud_run_recovery.py \
  --execution-id "exec_..." \
  --job-resource "projects/PROJECT_ID/locations/asia-northeast1/jobs/WORKER_JOB"
```

このコマンドはFirestoreのexecution/outbox状態を確認し、Cloud Run Executions一覧を全ページ走査して、注文ID・execution ID・slot IDの3つが一致するJobを検索する。表示するのはExecution名・作成/開始/完了時刻・task countだけで、引継ぎコードやPINなどは取得・表示しない。

- `matching_execution_found`: 対応するJob Executionが存在する。Cloud Run Console/Logsと注文の状態を照合する。
- `multiple_matching_executions_found`: 複数一致。二重起動の可能性を調査し、slot/lockは保持する。
- `no_matching_execution_found`: 一致が見つからなかっただけで、Job APIが未受理だった証拠ではない。APIの反映遅延やExecution retentionもあるため、slot/lockを解除しない。

**このコマンドは読み取り専用であり、slot解放・transfer lock解除・Job再実行は一切行わない。** no-matchを根拠に手動解除もしない。別途Cloud Run ConsoleのExecutions、Cloud Audit Logs、Firestoreのorder/execution/outboxを照合し、二重実行がないと確証を得るまでreviewを維持する。


## 22. Dispatcher operation名のみ保存された曖昧起動

Cloud Run Job起動のlong-running operation名はFirestoreに保存できたが、Execution名がまだ取得できない状態が600秒以上続く場合、reconcilerは `launching` / `dispatched` のどちらからでも注文・executionをreview/outcome_unknownへ移す。slotは解放せず、transfer lockがある場合も保持する。その後はsection 19の読み取り専用InspectorでExecution引数を照合する。


## 23. Runtime service account IAM

実環境を作成する前に [Cloud Run IAM matrix](CLOUD_RUN_IAM_MATRIX.md) を確認する。Web / Dispatcher / Worker / Cloud Tasks OIDC / Scheduler OIDCはidentityを分離する。runtime service accountへOwner / Editor / Project-wide run.admin / Project-wide Secret Accessorを付与しない。Firestore server SDKは `roles/datastore.user`、Inspectorはread-onlyの `roles/datastore.viewer` を使い、Secret Managerは個別Secret単位で付与する。

Dispatcherには対象QueueのCloud Tasks Enqueuerと対象Worker Jobの実行権限、Executions照合用のread権限だけを付与する。Cloud Tasks/Scheduler OIDC identityはPrivate Dispatcher ServiceのInvokerのみ。実際のIAM Policy Simulatorと実リソース単位の権限テストが完了するまで本番切替しない。
