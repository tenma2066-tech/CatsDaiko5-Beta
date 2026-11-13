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
