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

- 公開トップは `/` → `/shop`。購入者アカウントやログインは不要。
- 管理者用セーブ処理画面は `/console`（既存の `/login` で認証）。
- 管理者ログイン後、`/admin` で管理者モードを解除し、`/admin/paypay` でPayPay接続、`/admin/orders` で注文を確認する。
- PayPayのSMS認証はPayPay側が要求した場合だけ行う。SMSコードは管理画面から入力する。
- 商品はRender環境変数 `SHOP_CATALOG_JSON` にJSON配列で設定する。例：
  `[{"id":"sample-service","title":"サンプル商品","price_yen":1000,"description":"説明"}]`
  実際の販売商品名・価格に置き換えること。金額はサーバー側カタログを正とし、ブラウザーから送られた金額は使用しない。
- `PAYPAY_ENCRYPTION_KEY` はFernet形式のキーを一度生成してRenderのSecretとして設定し、以後変更しない。変更すると保存済みのPayPay認証情報と注文内の引継ぎ情報を復号できなくなる。
- `PAYPAY_DATA_DIR`、`PAYPAY_LEDGER_PATH`、`SHOP_DB_PATH` は同一のベータ専用永続ディスク上に置く。**永続ディスクを接続するまでは実決済を受け付けないこと**。Render Freeの一時ファイル領域では再起動・再デプロイ時に注文や認証情報が失われる可能性がある。
- 安定版のディスク、PayPayアカウント情報、暗号化キー、DBは共有しない。
- 注文の引継ぎコード・暗証番号は暗号化して保存し、公開注文照会APIには返さない。管理者の注文画面だけで確認できる。
- PayPayリンク受取の結果が不明・一時保留・重複の場合は注文を確認待ちにし、同じリンクを自動再試行しない。PayPayアプリ側の状態を確認してから管理者が手動で判断する。
- このゲートウェイはアップロードされたDiscord版の非公式WebプロトコルをWeb向けに移植したもの。PayPay公式SDK/APIではなく、仕様変更・利用制限・アカウント制限のリスクがある。実決済前に少額のテストを行い、決済結果をPayPayアプリでも確認すること。
