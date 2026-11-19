# xbot（X の予約投稿・DM自動返信）引き継ぎメモ

最終更新：2026-11-19

## 構成
- 管理サイト：https://tnshop-xbot.duckdns.org/ （パスワードは GitHub Secret `XBOT_ADMIN_PASSWORD`）
- VPS（にゃんこ代行と同じ）：`/opt/xbot`（プログラム・venv・ブラウザ）、`/var/lib/xbot/data`（DB・画像・記録・ブラウザのプロフィール。chmod 700）、`/etc/xbot/env`
- サービス：`xbot-web`（管理サイト・gunicorn・docker0 の IP:8090）、`xbot-worker`（常駐。予約投稿・DM・ログイン取り込み）
- お店の Caddy が `/opt/catsdaiko/caddy-sites/xbot.caddy` を読み込む（reload のみ）
- 更新：`xbot/deploy/run.txt` を書き換えて push → `.github/workflows/xbot-deploy.yml`
  - 1行目 `install`（設置・更新）／`status`（状態とログ。DM の相手名は伏せ字）／`probe`（DM画面の診断）／`caddy`（2行目にドメイン）／`dmcheck`（下書きモードのときだけ DM 確認を1回）
  - Actions のログは公開されるので、DM の相手名・ID を出さないこと
- お店の更新（vps-deploy）は `xbot/**`・`.github/workflows/xbot-*.yml`・`docs/**` では動かない

## ログインまわり（最終形）
- X は、サーバーからのログイン操作や、別の端末で取った Cookie だけの利用を切ってしまう。次の形で安定した。
  - iPhone の Orion＋Cookie-Editor で `auth_token` などを取り出し、管理サイトに貼る（パスワードはサーバーに渡さない）
  - サーバーのブラウザは「iPhone（WebKit）」＋ Cloudflare WARP（プロキシモード・xbot だけ。127.0.0.1:40000）
- DM（X の新しいチャット /i/chat）はスマホ版では表示されないため、Chromium（パソコン）で開く。開くたびに iPhone 側の Cookie を写す（`DMBrowser`）
- チャットは暗号化されていて、新しい端末では4桁の暗証番号が要る（管理サイトの「チャットの暗証番号を入力する」で一度だけ。保存しない）

## DM の仕組み（browser.py / worker.run_dms）
- 画面の目印（data-testid）は Shadow DOM の中。Playwright の locator はそのまま届く
  - 一覧 `dm-conversation-item-<ID>:<ID>`、リクエスト `dm-message-request-item-<ID>:<ID>`、許可 `dm-message-request-accept-button`
  - 「その他」（隠れたリクエスト）`dm-message-requests-other-button`、本文 `message-text-<uuid>`、入力 `dm-composer-textarea`、暗証番号 `pin-code-input-container`
- 最後のメッセージが相手からか：本文の要素が一覧の右寄り（自分）か左寄り（相手）か
- 同じメッセージには二度返さない（replied テーブル）。下書きモードの記録は本番の返信を止めない。ルールを変えたら、当てはまらなかった分も見直す

## 未解決（2026-11-19 時点）：ふつうの会話への返信
- 症状：記録が「会話を開けませんでした（TimeoutError）」。リクエストの許可・返信と予約投稿は動いている
- 分かったこと（probe で確認）
  1. リクエスト画面を開いたあとは、同じブラウザで会話の一覧が0件になる → 会話を先に見る順にした（一覧は12件出るようになった）
  2. 会話を1件開くと、同じブラウザでは2件目以降が開かない（about:blank や再読み込みでも直らない。ブラウザを起動し直すと直る。チャットの暗号まわりの常駐処理が原因と推測）
  3. 新しいブラウザで会話のURL（/i/chat/ID-ID）をいきなり開いても入力欄が出ない
- 最後に入れた修正（未確認）：会話ごとに新しいブラウザを起動 → まず一覧（/i/chat）を開く → 会話のURLを開く（commit 1333b49「会話を開く前に、新しいブラウザでまず一覧を開いて…」）
- 次にやること：`status` で記録を見て、「会話を開けませんでした」が消えたか確かめる。だめなら、会話を開いたときの画面の記録（shots）とテストID（数字は伏せる）を probe で調べる
  - 別案：一覧の項目をクリックして開く（最初の1件はこれで開けていた可能性）／会話ごとにブラウザを起動し、一覧→クリック

## その他の注意
- 設置時の apt は、VPS の自動アップデートとぶつかると止まる → パッケージが入っていれば apt を使わない・ロック待ちは最大5分にした
- 設置時のブラウザ確認は使い捨てのプロフィールで行う（動いている常駐とプロフィールを取り合わない）
