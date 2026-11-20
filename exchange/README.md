# 換金 Discord BOT の VPS 版

店舗と xbot が動作する Ubuntu 24.04 VPS の独立した systemd サービスです。Discord への接続は外向きで、ドメイン・公開ポート・Caddy の設定は不要です。

| 用途 | 場所 |
|---|---|
| 実行コード・専用 venv | /opt/paykyash/current |
| 設定・注文・利用済みリンク・本人確認・決済セッション | /var/lib/paykyash |
| Discord トークンと管理者 ID | /etc/paykyash/secrets.json |
| サービス・専用ユーザー | paykyash.service・paykyash |

## 初回設置

1. リポジトリのルートから VPS 上で `sudo bash exchange/deploy/install.sh` を実行します。main に統合した後は GitHub Actions の「Exchange VPS」で install を選んでも設置できます。Actions は xbot と同じ VPS_HOST・VPS_PASSWORD を使います。SSH はパスワード認証で、各ランナーの初回接続時に受け取ったホスト鍵を信頼します。install はモックテスト後にリリースを切り替えます。サービスの起動は別操作です。
2. VPS 上で `sudoedit /etc/paykyash/secrets.json` を開き、Discord アプリの bot_token と管理者の admin_id を設定します。BOT を対象サーバーに bot・applications.commands スコープで招待します。
3. 旧 BOT がある場合は受付を停止し、実行中の取引が終わったことを確認して旧プロセスを停止します。旧 system フォルダをバックアップし、運用中の JSON と config.json.bak をひとまとまりで /var/lib/paykyash に移します。注文・使用済みリンク・本人確認・保留・セッションを同じ時点で引き継いでください。secrets.json と *.example.json は除外します。移したファイルの所有者は paykyash:paykyash、権限は 0600 にします。
4. PayPay は移行済みの有効なセッションとその更新に対応します。旧版のブラウザによる WAF 回避を含む新規認証は移植していません。初回認証や更新不能時の再認証には、別途 PayPay が提供・承認する認証経路の実装が必要です。
5. `sudo systemctl enable --now paykyash` で起動します。`systemctl is-active paykyash` でプロセス状態を確認し、Discord の /ヘルス、/注文一覧、/保留一覧で移行結果を確かめます。/ログ設定 で通知先を指定し、/パネル で利用者向けの入口を設置できます。

## 更新

Discord の /設定 から受付を停止し、注文と保留の状態を確認してから `sudo systemctl stop paykyash` を実行します。設置手順を再実行し、`sudo systemctl start paykyash` と /ヘルス で確認後、受付を再開します。設置済みリリースは /opt/paykyash/releases に残ります。送金結果が不明な UNCERTAIN 注文は明細と突き合わせて /送金確認 で処理します。

店舗の自動デプロイは店舗コードの更新で動きます。`.dockerignore` と店舗のデプロイワークフローだけを変更した場合、即時反映が必要なら店舗の「Deploy to VPS」を手動実行します。

## 決済の復旧

- `RECEIPT_REVIEW` は Kyash の着金結果が不明な注文です。管理者が受取明細・金額・送信者を当該注文と照合してください。着金が確認できた場合だけ、理由を残して `/強制状態変更` で HOLD に変更し、払い出し先を確認して `/再送` してください。未着金が確定した場合は CANCELED にします。
- `UNCERTAIN` は送金・返金の成否が不明な注文です。Kyash 明細が注文固有の UUID を返す場合は `/送金確認` で自動照合できます。旧注文や UUID が提供されない明細は手動照合が必要です。
- `orders.json` の異常・初期化後の消失では注文処理を停止します。サービスを止め、原因を確認して同じ時点の台帳一式を復元してください。バックアップには /var/lib/paykyash 全体（orders.json.initialized を含む）を保存します。

## ローカル検証

exchange ディレクトリで以下を実行します。取引テストは一時ディレクトリと外部サービスのモックを使用します。

```sh
python3 -m venv /tmp/paykyash-test
/tmp/paykyash-test/bin/pip install -r requirements.txt
/tmp/paykyash-test/bin/python system/tests/test_flows.py
/tmp/paykyash-test/bin/python system/tests/test_recovery.py
```
