# AI 向け作業ガイド（AGENTS.md）

このリポジトリを編集する AI（Claude・Codex・Cursor など）は、作業の前に必ずこのファイルを読むこと。
**`main` は本番そのもの**。`main` に push すると、そのまま本番サーバー（利用者がいるお店）に反映される。

---

## 1. 何が入っているか

| システム | 中身 | 本番 |
|---|---|---|
| **にゃんこ代行**（お店） | `app.py`（Flask・画面も中に入っている）、`engine.py`（bcsfe でセーブ編集）、`paypay_gateway.py`（PayPay 受け取り・台帳・クーポン）、`event_maps.json`（イベステ一覧） | https://nyanko-daiko-tnshop.duckdns.org/ （管理画面 `/admin`） |
| **xbot**（X の予約投稿・DM自動返信。オーナー専用） | `xbot/xbot/`（web.py＝管理サイト、worker.py＝常駐、browser.py＝Playwright、xlogin.py、logic.py、store.py、page.html） | https://tnshop-xbot.duckdns.org/ |

- サーバー：国内 VPS（Ubuntu 24.04・4GB）。お店は Docker（`/opt/catsdaiko`、app ＋ Caddy）。xbot は systemd（`/opt/xbot`、データ `/var/lib/xbot`）。
- 詳しい経緯：`docs/BETA_FLY_PAYPAY.md`（お店・番号付きの変更履歴）、`docs/XBOT_NOTES.md`（xbot）、`docs/VPS_DEPLOY.md`。

## 2. 本番への反映（デプロイ）

| 何を変えた | どうなる |
|---|---|
| お店のファイル（`app.py` など） | `main` に push → `.github/workflows/vps-deploy.yml` が自動で反映。**実行中・決済中の注文が終わるまで最大12分待ってから**入れ替える（利用者を弾かない） |
| `*.md`・`docs/**`・`xbot/**`・`.github/workflows/xbot-*.yml` | お店のデプロイは動かない |
| xbot | `xbot/deploy/run.txt` を書き換えて push したときだけ `xbot-deploy.yml` が動く（Actions 画面から手動実行も可） |

`xbot/deploy/run.txt` の1行目：
- `install` … xbot を入れる・更新する（サービス再起動）
- `status` … 状態とログ（DM の相手名は伏せ字）
- `probe` … DM 画面の診断（読むだけ）
- `dmcheck` … 下書きモードのときだけ DM 確認を1回
- `caddy`（2行目にドメイン）… 管理サイトの URL を Caddy に追加（reload のみ）

2行目以降は何でもよい（同じ内容だと push しても変化が無いので `#番号` を変えて使う）。

反映後の確認：`curl https://nyanko-daiko-tnshop.duckdns.org/health`（200）、`/api/state`（`running_jobs`・`paying_orders` など）。xbot は `status`。

## 3. 守ること（重要）

1. **利用者がいる**。決済（PayPay）と注文の流れを壊さない。料金・注文・支払い状態を変えるときは、テストを足してから。
2. **テストを全部通してから push**：
   ```
   for t in test_new_features test_order_flow test_hold_e2e test_improvements test_item_types test_parallel test_paypay_hold test_paypay_gateway; do python3 -m unittest $t; done
   python3 selftest.py            # 「NG 0件」であること
   python3 xbot/test_logic.py     # xbot を触ったとき
   ```
   画面（`app.py` の中の HTML/JS）を変えたら、ブラウザで実際に動かして、**画面の金額とサーバーの金額が一致する**ことも確かめる。
3. **リポジトリは公開**。パスワード・トークン・利用者の情報・DM の相手名・X/PayPay の ID をコミットしない。GitHub Actions のログも公開されるので、ログに出さない（xbot の `status` は伏せ字にしてある）。
4. **秘密情報は GitHub Secrets だけ**：`VPS_HOST`、`VPS_PASSWORD`、`ADMIN_PASSWORD`、`SITE_DOMAIN`、`XBOT_ADMIN_PASSWORD`（ほか Fly 用）。チャットに貼らせない。
5. **料金は管理画面から変えられる**（`PRICES`、`prices.json`）。コードで勝手に値段を変えない。
6. **支払い済みの古い注文が残っていることがある**。メニュー名やデータの形を変えるときは、古い名前でも実行できるように残す（例：`engine.py` の旧レジェンド名、`EVENT_BY_ID` のコラボ分）。
7. 依頼主の確認なしに、本番での投稿・DM 送信・決済のテストをしない。

## 4. お店の主な仕組み

- 注文の状態：`unpaid → paying → hold → paid → starting → started → done`（ほかに `dead`）。注文は `orders.enc`（暗号化）、支払い・履歴・クーポンは `payments.sqlite3`（`PAYPAY_DATA_DIR`）。
- 料金は**サーバーで計算**（`compute_amount`）。画面の見積もり（JS の `estimate` / `renderCart`）はそれに合わせているだけ。
- 実行は別プロセス（multiprocessing spawn、最大4並列）。ゲームのサーバーへの通信はタイムアウト付き。
- メニュー：アイテム（数量指定）／キャラ／サブ（レジェンドは段階式・`engine.LEGEND_TIERS`）／イベステ（`event_maps.json`、前提 `reqs`・ドロップ `drops`）／その他。安全モード・試運転は廃止済み。
- 管理画面 `/admin`：ホーム・注文・履歴・PayPay・設定（料金・メンテナンス・リスタートパック・クーポン）。クーポンは2種類：完了時に自動で出る10%引き（`NK-`）と、管理者が発行する1回だけ無料（`FR-`）。

## 5. xbot の注意

- X は API を使わずブラウザ操作。画面の目印（`data-testid`）は X の都合で変わる。
- ログインは iPhone で取った Cookie を管理サイトに貼る方式。サーバーのブラウザは iPhone（WebKit）＋ Cloudflare WARP（xbot だけ）。DM は Chromium。
- **ふつうの会話への自動返信は未解決**（`docs/XBOT_NOTES.md` の「未解決」）。リクエストの許可・返信と予約投稿は動いている。直すときは、失敗し続けて予約投稿を止めないこと（以前、一晩止めてしまった）。

## 6. ブランチ

- 本番：`main`（2026-11-20 から）。それまでの作業ブランチは `claude/zealous-ritchie-5o2epn`（同じ履歴）。
- 大きな変更は別ブランチで作って、テストしてから `main` へ。
