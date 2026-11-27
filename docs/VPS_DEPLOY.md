# VPS（Ubuntu）へのデプロイ

GitHub Actions（`.github/workflows/vps-deploy.yml`）が、SSH（パスワードまたは鍵）でサーバーに入り、
Docker（アプリ + Caddy）で起動する。HTTPS は Let's Encrypt で自動（独自ドメインがなければ `<IPの.を->.sslip.io`）。

## サーバー側の前提
- Ubuntu 24.04（root で入れること）、メモリ 2GB 以上推奨
- **サーバー管理画面のパケットフィルターで 22（SSH）・80（HTTP）・443（HTTPS）を許可**（ルールを1つでも有効にすると、他は全部閉じる）

## GitHub のシークレット（Settings → Secrets and variables → Actions）
| 名前 | 必須 | 内容 |
|---|---|---|
| `VPS_HOST` | ◯ | サーバーの IP アドレス |
| `VPS_PASSWORD` | ◯（鍵を使わない場合） | サーバーの root パスワード |
| `VPS_SSH_KEY` | 鍵を使う場合 | 秘密鍵（あるとパスワードより優先） |
| `ADMIN_PASSWORD` | ◯ | 管理画面（/admin）のパスコード |
| `VPS_USER` | 任意 | 既定 `root` |
| `SITE_DOMAIN` | 任意 | 独自ドメイン（なければ sslip.io） |
| `DYA_PIN` | 任意 | ミニゲーム（ダイナマイト野球・`/dya/`）の暗証番号。数字4〜12桁。**無ければミニゲームは動かさない** |

## ミニゲーム（ダイナマイト野球）
- ミニゲーム一式はこのリポジトリの `dya/`（tenma2066-tech/dya-proxy の写し）。別コンテナ `dya`（Node）で動かす。お店の `app` とは別のコンテナで、注文・台帳・`data/` には触れない。お店のイメージには入れない（`.dockerignore`）。
- Caddy が `/dya/*` の先頭 `/dya` を外して `dya:8080` に回す（`deploy/vps/Caddyfile`）。`proxy.js` は `DYA_BASE_PATH=/dya` で、リンク・Cookie・中継 URL に `/dya` を付けて返す。
- 暗証番号（Render と同じ `DYA_PIN`）の画面を通らないと遊べない。Render 版の全機能（オンライン・オフライン・チート・保存データ・オリジナルチーム・編集ツール）をそのまま配る。
- ソース（`src/dya/proxy.js`）と `DYA_PIN` がそろったときだけ、`remote-setup.sh` が `.env` に `COMPOSE_PROFILES=dya` と `DYA_ENABLED=1` を書く。そろわなければ `dya` を止め、トップのカードも隠す。
- ミニゲームを直すときは `dya/` を直接直して main に入れれば、ふつうのデプロイで反映される。

## 動き
1. 接続確認（OS・メモリを表示）
2. ソースを送る → サーバーで Docker を入れ、鍵（SECRET_KEY / PAYPAY_ENCRYPTION_KEY）を**初回だけ**自動生成
3. 先にビルド（サイトは動いたまま）→ 実行中・決済確認中がゼロになるまで待つ（最大12分）→ 入れ替え → 実行の受付を再開
4. `https://<IP>.sslip.io/health` で動作確認。失敗したらコンテナの状態を出す

## データの場所（サーバー内）
`/opt/catsdaiko/`（root のみ）: `.env`（秘密鍵）, `admin_password`, `data/`（PayPay 認証情報・注文・台帳）, `src/`
- **`.env` の `PAYPAY_ENCRYPTION_KEY` を失うと、保存済みの PayPay 情報・注文が読めなくなる。**
- サーバーを作り直す・削除すると、`data/` も消える。

## 動作確認済みの範囲
- サンドボックスの Docker で、初回・2回目（変更なし／あり）の実行、鍵が変わらないこと、管理者パスワードの保持、受付の再開を確認。
- 本物のVPS上でのDocker導入・Let's Encrypt の証明書取得・SSH接続は、初回デプロイで初めて確認される。
