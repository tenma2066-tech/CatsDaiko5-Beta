# PayPay / Kyash 換金管理（Web）

管理者本人がスマートフォンや PC のブラウザから使うサービスです。ログイン後に換金、手数料の見積もり、注文履歴、接続・本人確認の状態を表示します。Discord の設定は不要です。

## VPS への設置

GitHub Actions の「Exchange VPS」は店と同じ VPS_HOST・VPS_PASSWORD で VPS に接続します。

1. main で action=install を実行します。テスト後に paykyash.service を登録し、Docker の内部アドレスの 8091 番で起動します。データは /var/lib/paykyash、コードは /opt/paykyash/current です。
2. GitHub のリポジトリ Secret に EXCHANGE_ADMIN_PASSWORD を設定し、action=password を実行します。VPS には照合用ハッシュを保存します。設定前はログインできません。
3. DuckDNS で換金専用ホストの IPv4 を VPS に向けます。action=domain の domain 欄にホスト名を入力すると、DNS を確認し、既存 Caddy に専用サイトを追加して HTTPS を確認します。
4. 専用 URL にアクセスしてログインします。接続・設定画面から Kyash の認証、本人確認、換金受付と自動払い出しの切り替えができます。PayPay は既存の有効なセッションを利用します。新規 PayPay 認証は未対応で、両方の接続と送金元の登録が揃うまで換金は実行できません。

## 運用

送金結果が不明な注文は UNCERTAIN として停止します。取引履歴で本人が該当明細を照合した後に結果を記録できます。Kyash の受取中に停止した注文は RECEIPT_REVIEW で保留し、自動払い出しを止めます。注文台帳の破損や消失時はサービスを停止して同時点の /var/lib/paykyash バックアップを復元してください。ダウンロードした旧 Discord 版を同じデータで並行起動しないでください。

更新時は受付を停止し、実行中の取引が終了した後、VPS で systemctl stop paykyash を実行してから install を再実行します。既存のパスワードとホスト設定は引き継がれます。
