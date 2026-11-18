# xbot（X の予約投稿・DM自動返信）

X の API は使わず、Playwright（Chromium）でブラウザを自動操作します。操作はすべて iPhone の管理サイトで行います。

## 置き場所（VPS）
| もの | 場所 |
|---|---|
| プログラム | `/opt/xbot/xbot/`（venv は `/opt/xbot/venv`、Chromium は `/opt/xbot/browsers`） |
| データ（予約・ルール・画像・記録） | `/var/lib/xbot/data/`（`xbot.sqlite3`、`uploads/`、`shots/`） |
| ログイン情報 | `/var/lib/xbot/data/profile/`（chmod 700・ユーザー xbot だけ） |
| 設定 | `/etc/xbot/env`（管理パスワードなど。root と xbot だけが読める） |
| サービス | `xbot-web.service`（管理サイト）、`xbot-worker.service`（常駐） |

## コマンド（VPS で root）
```
systemctl status xbot-web xbot-worker      # 状態
systemctl stop xbot-worker                 # 止める（管理サイトの「一時停止」でも止まる）
systemctl start xbot-worker                # 起動
systemctl restart xbot-worker              # 再起動
journalctl -u xbot-worker -f               # ログを見続ける
journalctl -u xbot-web -n 100              # 管理サイトのログ
```

## 使い方（管理サイト）
- **予約投稿**：本文・画像（4枚まで）・日時（日本時間）を入れて「予約する」。
- **DM返信**：ルール（すべてのDM／キーワード／はじめての相手）と返信する文を登録し、「DM自動返信をオン」。
- **設定**：下書きモード⇔本番モード、上限（1日の投稿数・1時間の返信数・間隔）。
- **ホーム**：一時停止、ログイン確認、画面の記録、X へのログイン画面（15分）。

最初は **下書きモード**（投稿・送信ボタンを押さない）で動きます。

## テスト
`python test_logic.py`（X には接続しません）
