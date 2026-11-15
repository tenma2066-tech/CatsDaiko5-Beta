# Beta（Fly.io + PayPay決済）引き継ぎメモ

最終更新: 2026-11-15 / ブランチ: `claude/zealous-ritchie-5o2epn`（Beta リポジトリ）

## 1. 目的と全体像
- CatsDaiko5（安定版）の内容をベースに、Beta は **ログインを廃止し、PayPay決済が確認できてから実行**する個人利用版。
- 公開URL: https://catsdaiko5-beta.fly.dev/ （管理画面は `/admin`）
- 実行環境: Fly.io（東京 `nrt`、1台、メモリ1GB、常時起動）。Renderは使わない。

## 2. 経緯（時系列）
1. CatsDaiko5 の全内容を Beta にミラーコピー（このとき Beta 固有だった PayPay 実装が一度消えた）。
2. zip（Discord販売Bot）の PayPay 受取方式を参考に、以前の実装（`paypay_gateway.py` / `github_runtime_store.py`）を履歴から復元して流用。
3. ログイン廃止 → 「実行」で注文作成 → 送金リンク決済 → 確認後に自動実行。
4. Render → Fly.io へ移行（Dockerfile / fly.toml / ボリューム）。端末がないため **GitHub Actions** でデプロイ。
5. 料金ラインナップ導入、バン保証・アカウント複製をサブタブへ移動。
6. 支払い画面をモーダル化、エラー細分化、使い方ボタン追加。
7. 保留（42007013〜16）・結果不明の注文を、同じリンクの再確認で復帰できるようにした。
8. 注文を暗号化して永続化（再起動しても支払い済み・保留が残る）。

## 3. 動作の流れ（顧客側）
1. 項目を選び「決済して実行」→ `POST /run` は**実行せず注文を作る**（金額はサーバーが計算）。
2. 支払いモーダルに金額・内訳が出る → PayPayで表示金額ちょうどの送金リンク（パスコードなし）を貼る。
3. `POST /api/orders/<id>/pay` → リンク検証（金額・期限・未受取など）→ 受け取り → ジョブ開始。
4. 状態: `unpaid → paying → paid → started`。保留/結果不明は `hold`、復帰不能は `dead`。
5. `hold` のとき: PayPayアプリで該当の送金を「受け取る」→ 画面の「受け取りを済ませたので確認する」→ 同じリンクが COMPLETED なら自動で開始。新しいリンクは受け付けない。
6. 支払い済みで待機枠が満杯（429）の場合は、再送金なしでボタンを押し直せば開始できる。

## 4. 料金（`app.py` の `PRICES` / `compute_amount`）
| 内容 | 料金 |
|---|---|
| キャラ指定（1体ごと） | 50円 |
| クラッシュ解除（ガマトト・オトート砲台） | 1件50円 |
| バン保証（旧セーブ保全チェック、サブ→特別メニュー） | 500円 |
| アカウント複製（サブ→特別メニュー） | 100円 |
| それ以外（アイテム・数量指定・サブ・その他） | 1件20円 |
- 試運転も同額。バン保証/複製は単独料金（他項目は使わない）。
- `PAYMENTS_ENABLED=0` で決済なし（動作確認用）。

## 5. 主なファイル
- `app.py` … 注文API（`/run`, `/api/orders/<id>/pay`）、`/admin`（PayPay接続）、UI（支払いモーダル・使い方）、注文の永続化
- `paypay_gateway.py` … PayPayログイン/OTP、リンク検証・受け取り、台帳(SQLite)、`confirm_received`、`save_orders/load_orders`
- `github_runtime_store.py` … （Render時代の）GitHub保存。Fly では `RUNTIME_GITHUB_REPO` 未設定なのでボリュームを使う
- `Dockerfile` / `fly.toml` / `.github/workflows/fly-deploy.yml`
- テスト: `test_order_flow.py`（注文・料金・保留復帰・永続化）、`test_paypay_gateway.py` ほか

## 6. 運用
- デプロイ: `claude/zealous-ritchie-5o2epn` への push で自動（`*.md` と `docs/**` のみの変更は対象外）。手動は Actions →「Deploy to Fly.io」。
- GitHub Secrets: `FLY_API_TOKEN`（orgトークン）, `ADMIN_PASSWORD`。`SECRET_KEY` と `PAYPAY_ENCRYPTION_KEY` は初回に自動生成して Fly secrets に保存（値は表示されない・上書きしない）。
- PayPay接続: `/admin` に ADMIN_PASSWORD で入り、受取側アカウントの電話番号/パスワード → SMS認証。接続済み。
- データ: ボリューム `data` → `/data/paypay`（アカウント `account.enc`、注文 `orders.enc`、台帳 `payments.sqlite3`）。`PAYPAY_ENCRYPTION_KEY` を失うと読めなくなる。
- 注文の保持: 支払い待ちは30分、保留/支払い済みは24時間。持ち主はブラウザの Cookie で判別（Cookie削除・別端末では見えない）。

## 7. 既知の注意点・未対応
- PayPayの非公式Web APIを使用。仕様変更で動かなくなる可能性。保留の実機での復帰フロー（`confirm_received`）は、テストはあるが本物のPayPayではまだ未検証。
- PayPayリンクの状態名（CANCELED/EXPIRED 等）への対応づけは推測を含む。想定外の表示があれば要調整。
- 台数は1・ワーカーも1のまま（ボリューム専用・メモリ上のジョブ管理のため）。
- **Fly.io のトライアル終了後はカード登録が必要**（目安: 月5〜8ドル）。カードがない場合の案: バーチャルのプリペイドカード（Kyash等）でFlyを継続／日本のVPS（ConoHa等）へ移行（GitHub Actions経由のSSHデプロイで対応可能）。未決定。
- 以前この環境に保存した `api.fly.io` のトークンは使えていない。Fly側で失効させておくこと。
- zip に含まれていた Discord トークン等は使っていない。他人に渡した可能性があれば再発行。
- UI/UXは引き続き調整予定（ユーザー希望）。
- 安全モードの初期値はオフ（猫缶・レアチケ・プラチケ・プラチナのかけら・レジェチケも実行される。BANの恐れは利用者の自己責任）。オンにすると外され、料金にも含まれない。
