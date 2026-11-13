# Cloud Run / Firestore IAM権限マトリクス

本書はCloud Run移行時のruntime service account向け権限案。実Projectへ付与する前に、Cloud ConsoleのIAM Policy Simulatorまたは最小権限の検証Projectで各API呼び出しを確認する。**Owner / Editor / Project-wide Cloud Run Adminをruntime service accountへ付与しない。**

## Runtime identities

| Identity | 必要なアクセス | 付与スコープ | 与えない権限 |
|---|---|---|---|
| Web Service SA | `roles/datastore.user` | Project（Firestoreのデータアクセス） | Cloud Run Job実行、Cloud Tasks作成 |
| Web Service SA | `roles/secretmanager.secretAccessor` | 必要な個別Secretのみ | Project全体のSecret Accessor |
| Dispatcher Service SA | `roles/datastore.user` | Project | Secret Managerアクセス（不要なら付与しない） |
| Dispatcher Service SA | `roles/cloudtasks.enqueuer` | 対象Cloud Tasks Queue | Queue Admin / Tasks Admin |
| Dispatcher Service SA | `roles/run.jobsExecutorWithOverrides` | 対象Worker Jobのみ | Project全体のCloud Run Developer |
| Dispatcher Service SA | `roles/run.viewer` | 対象Worker Jobのみ | Project全体のCloud Run Viewer |
| Worker Job SA | `roles/datastore.user` | Project | Cloud Tasks作成、Cloud Run Job起動 |
| Worker Job SA | `roles/secretmanager.secretAccessor` | Workerが必要とする個別Secretのみ | 全Secretへのアクセス |
| Cloud Tasks OIDC SA | `roles/run.servicesInvoker` | Private Dispatcher Serviceのみ | Job実行・Firestore書込 |
| Cloud Scheduler OIDC SA | `roles/run.servicesInvoker` | Private Dispatcher Serviceのみ | Job実行・Firestore書込 |
| 手動Inspector用Operator | `roles/datastore.viewer` | Project | Firestore書込・Secretアクセス |
| 手動Inspector用Operator | `roles/run.viewer` | 対象Worker Jobのみ | Job実行・Job変更 |

## Role selection notes

- `roles/datastore.user` はFirestore/Datastoreのデータ読み書き用。Firestore IAMのこのroleはProject scopeで付与する。Firestore Security RulesはクライアントSDK向けのdeny-allを維持し、Cloud Run server SDKはIAMで制御する。
- `roles/cloudtasks.enqueuer` はTask作成用role（Beta）。対象Queueに限定して付与する。Queue作成・pause・purgeなどの管理権限はruntimeに与えない。
- `roles/run.jobsExecutorWithOverrides` はWorker Jobに対してだけ付与する。公式roleには実行キャンセル権限も含まれるため、将来さらに絞る場合は `run.jobs.run` / `run.jobs.runWithOverrides` の必要性を検証してcustom roleを検討する。
- `roles/run.viewer` はCloud Run Executionのget/list用。Inspector/Dispatcherには対象Worker Job resourceに限定して付与する。
- `roles/run.servicesInvoker` はPrivate Dispatcher ServiceへのHTTP invoke専用。Cloud TasksとSchedulerで別々のOIDC service accountを使う。
- `roles/secretmanager.secretAccessor` は個々のSecret resourceに付与する。Project-wideで付与しない。
- `roles/datastore.viewer` はInspector向けread-only role。実データmigrationの `--apply` 実行者には、作業時間中だけ `roles/datastore.user` を付与し、完了後に剥奪する。

## Secret assignment checklist

実際に必要なSecretは、Cloud Run Web / Dispatcher / Workerそれぞれのimport経路と起動テストを行って確定する。少なくとも次を個別に監査する。

- `PAYPAY_ENCRYPTION_KEY`: 既存暗号化データを復号するため、現行値を維持。再生成・変更禁止。
- `TRANSFER_LOCK_PEPPER`: WorkerのHMAC transfer lock用。独立したランダム値。Web/Dispatcherには不要なら渡さない。
- `SECRET_KEY`: Flask session等で使用する場合のみWebへ。
- `AUTH_GITHUB_TOKEN`: GitHub auth/runtime storeを使用するプロセスだけへ。
- `ADMIN_PASSWORD` 等の既存管理用secret: 実際のコード参照があるプロセスだけへ。

Secretの値を環境変数・GitHub・ログ・CI artifactへ書かない。Cloud RunのSecret Manager連携を使い、値は出力しない。

## Deployment operator

Cloud Run Service/Job、Firestore、Cloud Tasks、Secret Manager、Schedulerを作成する人間の権限はruntime identityと分離する。セットアップ時だけ必要な管理権限を使い、作成後はruntime SAに不要な管理権限が残っていないことを確認する。Service identityを指定してdeployするユーザーには、そのservice accountを使用するための `iam.serviceAccounts.actAs` が必要だが、runtime service account自身にOwner/Editorを付ける理由にはならない。

## Pre-deploy verification

- [ ] Firestore Rulesは全client read/write deny。
- [ ] Web / Dispatcher / Workerは別々のuser-managed service account。
- [ ] Cloud Tasks OIDC SAとScheduler OIDC SAは別。
- [ ] DispatcherだけがQueueへTaskを作成できる。
- [ ] Dispatcherだけが対象Worker Jobをoverride実行できる。
- [ ] Workerは対象Secretのみaccessできる。
- [ ] InspectorはFirestore read-only + 対象Job read-only。
- [ ] Project-wide Owner / Editor / run.admin / secretAccessorをruntime SAへ付与していない。
- [ ] IAM Policy Simulatorで実際の必要操作が成功し、不要な操作が拒否される。

## Official references

- [Cloud Run roles and permissions](https://cloud.google.com/iam/docs/roles-permissions/run)
- [Cloud Run access control](https://cloud.google.com/run/docs/securing/managing-access)
- [Firestore roles and permissions](https://cloud.google.com/iam/docs/roles-permissions/firestore)
- [Cloud Tasks roles and permissions](https://cloud.google.com/iam/docs/roles-permissions/cloudtasks)
- [Secret Manager access control](https://cloud.google.com/secret-manager/docs/access-control)
