# CatsDaiko5-Beta 運用メモ

## 目的
このリポジトリは `CatsDaiko5` の安定版から分離したベータ版です。ベータ版の変更を安定版へ自動反映しません。

## GitHub
- ベータ版リポジトリ: `tenma2066-tech/CatsDaiko5-Beta`
- ブランチ: `main`
- 認証データ保存先: このリポジトリの `auth/passwords.json`
- `AUTH_GITHUB_REPO` は必ず `tenma2066-tech/CatsDaiko5-Beta` に設定すること。

## Render
- Blueprint: `render.yaml`
- サービス名: `nyanko-web-beta`
- 安定版の `nyanko-web` とは別サービスとして作成すること。
- `SECRET_KEY`、`APP_PASSWORD`、`ADMIN_PASSWORD`、`AUTH_GITHUB_TOKEN` はベータ用に個別発行・設定すること。
- `AUTH_GITHUB_TOKEN` はベータ版リポジトリだけにアクセスできる最小権限のトークンを使うこと。
- 安定版とベータ版で同じ永続ディスク、DB、外部ストレージ、認証トークンを共有しないこと。
- Render側でBlueprintを適用する際は、既存の安定版サービスを変更・置換しないことを確認する。

## 開発ルール
1. ベータ版の機能追加・実験はこのリポジトリで行う。
2. CIと回帰テストを通してから実機確認する。
3. 安定版へ反映するときは、差分を確認して安定版側へ別途適用する。自動同期しない。
4. ベータ版のセーブデータは安定版と混在させない。


## 公開ショップ・PayPayベータ設定

- 公開トップは `/` → `/shop`。購入者アカウントやログインは不要。注文の順番は **商品選択 → PayPay決済 → 決済確認後に引継ぎコード・暗証番号入力**。
- 管理者用セーブ処理画面は `/console`（既存の `/login` で認証）。
- 管理者ログイン後、`/admin` で管理者モードを解除し、`/admin/paypay` でPayPay接続、`/admin/orders` で注文を確認する。
- PayPayのSMS認証はPayPay側が要求した場合だけ行う。SMSコードは管理画面から入力する。
- `SHOP_LIVE_ORDERS_ENABLED` は初期値 `0`。ベータ専用永続ディスク・暗号化キー・商品カタログ・PayPay接続の確認が完了するまで `1` にしない。無効中は公開ページを閲覧できるが、注文・決済・PayPay接続を受け付けない。
- 初期商品は「キャラ特定選択」50円/体、「その他の代行」25円/体。数量を指定するとサーバー側で単価×数量を計算する（1〜100体、合計100万円以下）。
- 商品を変更する場合だけRender環境変数 `SHOP_CATALOG_JSON` にJSON配列を設定する。例：
  `[{"id":"character-specific","title":"キャラ特定選択","unit_price_yen":50},{"id":"other-service","title":"その他の代行","unit_price_yen":25}]`
- 金額はサーバー側カタログを正とし、ブラウザーから送られた金額は使用しない。
- `PAYPAY_ENCRYPTION_KEY` は `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` で一度生成し、RenderのSecretとして設定して以後変更しない。変更すると保存済みのPayPay認証情報と注文内の引継ぎ情報を復号できなくなる。
- `PAYPAY_DATA_DIR`、`PAYPAY_LEDGER_PATH`、`SHOP_DB_PATH` は同一のベータ専用永続ディスク上に置く。現行 `render.yaml` はFreeプランでdisk定義もないため、このままでは永続化されない。Render Free Web Serviceは永続ディスクを利用できない。管理者が所有・管理するRender Workspaceで有料Web Serviceへ変更し、`/var/data` にdiskを接続してから実決済を有効化すること。課金が発生する変更は管理者がRender Dashboardで確認して実行する。
- 安定版のディスク、PayPayアカウント情報、暗号化キー、DBは共有しない。
- 注文の引継ぎコード・暗証番号は暗号化して保存し、公開注文照会APIには返さない。管理者の注文画面だけで確認できる。
- 決済開始時にDBの条件付き更新で注文を `payment_processing` に確保し、同一注文への同時リクエストは1件だけ通す。PayPay受取台帳にも送金リンクdigestと注文IDのUNIQUE制約を設ける。
- SQLiteを使うためRenderは1サービスインスタンス・gunicorn 1 workerを維持する。永続ディスクは複数インスタンス間で共有できない。
- PayPayリンク受取の結果が不明・一時保留・重複の場合は注文を確認待ちにし、同じリンクを自動再試行しない。PayPayアプリ側の状態を確認してから管理者が手動で判断する。
- このゲートウェイはアップロードされたDiscord版の非公式WebプロトコルをWeb向けに移植したもの。PayPay公式SDK/APIではなく、仕様変更・利用制限・アカウント制限のリスクがある。実決済前に少額のテストを行い、決済結果をPayPayアプリでも確認すること。
