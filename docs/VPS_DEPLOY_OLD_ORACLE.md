# Oracle Cloud へのデプロイ（ブランチ `claude/oracle-cloud`）

Fly.io のトライアル終了後の移行先。GitHub Actions が SSH でサーバーに入り、Docker（アプリ＋Caddy）で起動する。
HTTPS は Caddy が Let's Encrypt で自動取得（独自ドメインがなければ `<IPの.を->.sslip.io` を使う）。

## 重要: 30日後も無料で使い続けるには
- 30日間の無料クレジットとは別に **Always Free** 枠がある。インスタンスは必ず Always Free 対象の形で作る。
  - おすすめ: **VM.Standard.A1.Flex（Ampere, ARM）1〜2 OCPU / 6〜12GB**（Always Free の範囲内）
  - 代替: VM.Standard.E2.1.Micro（1GB。メモリが少なめ）
- ホームリージョンは **Japan East (Tokyo)** か **Japan Central (Osaka)**（後から変更不可）。A1 は「容量不足（Out of capacity）」で作れないことがある。その場合は時間をおいて再試行。
- 30日後、有料アカウントへアップグレードしない限り、Always Free 以外のリソースは停止・削除される。

## 手順（iPhone のブラウザで）
1. **インスタンス作成**: Compute → Instances → Create instance
   - Image: **Ubuntu 22.04 / 24.04**（Canonical Ubuntu）
   - Shape: 上記の Always Free 対象
   - Networking: パブリックIPv4アドレスを割り当てる
   - SSH keys: 「**Generate a key pair for me**」→「**Save private key**」でダウンロード（.key ファイル。紛失すると入れなくなる）
2. **ポートを開ける**: インスタンス詳細 → Subnet → Security List → Add Ingress Rules
   - Source CIDR `0.0.0.0/0`、TCP、Destination Port `80`
   - 同じく `443`
   （OS 側のファイアウォールはデプロイ時に自動で開ける）
3. **GitHub Secrets**（`tenma2066-tech/CatsDaiko5-Beta` → Settings → Secrets and variables → Actions）
   | 名前 | 値 |
   |---|---|
   | `VPS_HOST` | インスタンスのパブリックIPアドレス |
   | `VPS_SSH_KEY` | ダウンロードした .key ファイルの中身すべて（`-----BEGIN ... KEY-----` から `-----END ... KEY-----` まで） |
   | `VPS_USER` | 省略可（既定 `ubuntu`） |
   | `ADMIN_PASSWORD` | すでに登録済みのものを共用 |
   | `SITE_DOMAIN` | 省略可（独自ドメインを使う場合のみ） |
4. **デプロイ**: `claude/oracle-cloud` ブランチへの push、または Actions →「Deploy to Oracle Cloud」→ Run workflow。
5. 完了後 `https://<IPの.を->.sslip.io/admin` で PayPay に再ログイン（SMS 認証）。

## サーバー上の構成
- `/opt/catsdaiko/`（root のみ）: `docker-compose.yml`, `Caddyfile`, `.env`（SECRET_KEY / PAYPAY_ENCRYPTION_KEY / SITE_HOST）, `admin_password`, `data/`（PayPayアカウント・注文・台帳）, `src/`
- `SECRET_KEY` と `PAYPAY_ENCRYPTION_KEY` は初回に自動生成し、以後は上書きしない。
- 再デプロイしても `data/` と鍵は残る。

## 動作確認（このブランチ作成時）
- サンドボックス上で `remote-setup.sh` を実行し、Docker でアプリ＋Caddy が起動、HTTPS 経由で `/health` と `/admin`（ファイル経由の管理者パスワード）を確認済み。
- 再実行で鍵が変わらないこと、`ADMIN_PASSWORD` 未設定時に既存パスワードが残ることを確認済み。
- 実際の Oracle Cloud・ARM インスタンスでの起動、Let's Encrypt 証明書の取得は未確認。
