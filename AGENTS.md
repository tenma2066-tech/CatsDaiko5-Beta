# AGENTS.md — このリポジトリで作業する AI 向けの手引き

Claude・Codex・Cursor など、このリポジトリを編集する AI は **作業の前に必ず全部読むこと**。
返答・コミットメッセージ・画面の文言は **日本語**。依頼主は iPhone から指示することが多く、専門用語はかみくだいて伝える。
Claude Code で作業する場合は、さらに **`docs/CLAUDE_GUIDE.md`**（Claude 向けの補足）も読むこと。

---

## 0. 最初に知っておくこと（5行）

1. **`main` ＝ 本番**。`main` に push すると、利用者がいるお店にそのまま反映される。
2. **リポジトリは公開**。秘密情報・利用者の情報・DM の相手名・各種 ID をコミットにもログにも出さない。
3. push の前に **テストを全部通す**（§5）。画面を変えたら **ブラウザで実際に動かす**。
4. 料金は管理画面から変えられる。**コードで値段を勝手に変えない**。
5. 依頼主の確認なしに、**本番での投稿・DM 送信・決済のテストはしない**。

---

## 1. このリポジトリにあるもの

| システム | 何か | 本番 URL | 動かし方 |
|---|---|---|---|
| **にゃんこ代行**（お店） | 「にゃんこ大戦争」のセーブデータ編集を代行する Web サービス。PayPay で支払うと自動で処理して引継ぎコードを返す。別の大メニューとして**ツムツム代行**（`/tsum`・`docs/TSUM.md`）もある。運営名 **TN SHOP**、連絡先 X `@paysms1212` | https://nyanko-daiko-tnshop.duckdns.org/ （管理画面 `/admin`） | VPS の Docker（`/opt/catsdaiko`、app ＋ Caddy） |
| **xbot**（オーナー専用） | X（旧Twitter）の予約投稿・定期投稿・DM 自動返信。X の API は使わず、サーバーのブラウザ（Playwright）で操作する | https://tnshop-xbot.duckdns.org/ | VPS の systemd（`xbot-web`・`xbot-worker`、`/opt/xbot`、データ `/var/lib/xbot`） |
| `exchange/` | オーナーが別に追加したサービス。**この手引きの対象外**。お店・xbot の作業では触らない | — | `exchange-deploy.yml` |

サーバー：国内 VPS（Ubuntu 24.04・メモリ4GB）。お店の Caddy が xbot の管理サイトも中継している（`/etc/caddy/sites/*.caddy` を読み込む）。

---

## 2. ファイルの地図

### お店
| ファイル | 中身 |
|---|---|
| `app.py`（約2,800行） | Flask 本体。画面は `templates/`、料金計算は `pricing.py`。注文 `/run`、支払い `/api/orders/<id>/pay`、管理画面 `/admin`、ジョブの並列実行、クーポン、初期垢作成（リスタートパック）など |
| `engine.py` | bcsfe（セーブ編集ライブラリ）を使った実際の編集。アイテム付与、キャラ解放・レベル・形態・本能、ステージクリア、レジェンド段階 `LEGEND_TIERS`・`clear_legend_crowns`、イベステ `clear_event_map`、施設レベル |
| `paypay_gateway.py` | PayPay の受け取り（非公式 API）、台帳（SQLite：`links`・`history`・`coupons`）、クーポン（10%引き `NK-`・無料 `FR-`） |
| `item_catalog.py` | アイテムの種類名（キャッツアイ・マタタビ・本能玉など） |
| `tsum_service.py`・`tsum_engine/` | **ツムツム代行**（もう1つの大メニュー。画面は `/tsum`）。`tsum_engine/` は依頼主の Discord 版から持ってきた素のスクリプトで、中身は変えない。くわしくは `docs/TSUM.md` |
| `event_maps.json` | イベントステージ一覧（ゲームデータ 15.7.2 から作成）。`reqs`＝前提、`drops`＝ドロップキャラ `[内部ID, 形態]`、`cat`＝イベント／コラボ（コラボは選べないが、古い注文のため実行用には残す） |
| `save_audit.py`・`save_repair.py` | バン保証（セーブの検査・修復） |
| `pricing.py` | 料金の計算（`compute_amount` など。サーバー側の正）。画面の見積もりはこれに合わせる |
| `templates/` | 画面の HTML・CSS・JavaScript（`main.html`＝利用者、`admin.html`＝管理画面）。Jinja のひな形 |
| `github_runtime_store.py` | 以前の Fly.io 時代の保存の仕組み（VPS では無効。PayPay の台帳を開くときに通る） |
| `static/` | 画像（ご利用の流れの図解 `flow.webp`／`flow.jpg`）。差し替えたら `templates/main.html` の `?v=` を上げる |
| `template_save.sav` | 初期アカウント用のひな形セーブ |
| `selftest.py`・`smoke_bcsfe.py`・`test_*.py` | テスト（§5） |
| `deploy/vps/` | 本番の `docker-compose.yml`・`Caddyfile`・`remote-setup.sh` |
| `scripts/` | bcsfe の更新確認・ゲームデータの確認（Actions から使う） |

### xbot（`xbot/`）
| ファイル | 中身 |
|---|---|
| `xbot/web.py` | 管理サイト（Flask）。ログイン、予約投稿、DM ルール、設定、Cookie 貼り付けでの X ログイン、チャット暗証番号 |
| `xbot/worker.py` | 常駐ループ。予約投稿 `run_post`、DM 確認 `run_dms`、ログイン画面 |
| `xbot/browser.py` | Playwright 操作。投稿 `post`、DM（`dm_open_inbox`・`dm_open_chat`・`dm_reply` など）、`DMBrowser` |
| `xbot/logic.py` | ブラウザを使わない判断（ルール判定・上限・時刻・文字数 `MAX_POST_LEN`＝25,000） |
| `xbot/store.py` | SQLite（`settings`・`posts`・`rules`・`replied`・`events`） |
| `xbot/page.html` | 管理サイトの画面 |
| `xbot/test_logic.py` | xbot のテスト |
| `xbot/deploy/` | `setup.sh`（設置）、systemd のサービス、`run.txt`（§4）、`probe.sh`・`dmcheck.sh`・`caddy-site.sh`・`warp.sh` |
| `xbot/lab/` | DM 画面の実験（読むだけ・送信しない）。`run.txt` の `lab` から動かす |

### 記録（読むと経緯が分かる）
| ファイル | 中身 |
|---|---|
| `docs/BETA_FLY_PAYPAY.md` | お店の変更履歴（番号付き）。大きな変更をしたら1行足す |
| `docs/XBOT_NOTES.md` | xbot の引き継ぎメモ（ログイン方式、DM の画面の目印、原因判明した問題） |
| `docs/XBOT_PLAN.md` | xbot の改善計画と DM の調査結果 |
| `docs/VPS_DEPLOY.md` | VPS の設置手順 |
| `docs/CLAUDE_GUIDE.md` | Claude Code で作業するときの補足（反映の確かめ方・`main` への入れ方・してはいけないこと） |
| `docs/AUDIT_PLAN.md` | お店のセキュリティ・不具合の精査結果と補修計画（P0〜P3）。直したら表を更新する |
| `docs/TSUM.md` | ツムツム代行の作り（メニュー・料金・アカウントの入れ方・画像認証・設定・更新のしかた） |

---

## 3. お店の仕組み

### 画面の流れ（利用者）
1. **メニュー**：「何をしますか？」から 通常／バン保証／初期垢作成／アカウント複製 を選ぶ
2. **アカウント**：引継ぎコードと暗証番号（初期垢作成は空欄）
3. **確認・支払い**：金額を見て、PayPay の送金リンクを貼る。クーポン欄あり
4. **実行・受取**：進み具合 → 完了画面で引継ぎコードと暗証番号。コードを見る前に感想ポスト（X）へのお願いが出る（リンクを押して20秒後に表示。`RV_WAIT`）

### 注文と支払い
- 注文の状態：`unpaid → paying → hold → paid → starting → started → done`（ほかに `dead`）。
- 注文は `orders.enc`（暗号化）、支払い・履歴・クーポンは台帳 `payments.sqlite3`（`PAYPAY_DATA_DIR`）。
- **金額はサーバーで計算**（`compute_amount`）。画面の見積もり（JS の `estimate`・`renderCart`・`payable`）はそれに合わせているだけ。**両方を必ず一致させる**。
- 実行は別プロセス（multiprocessing spawn、最大4並列）。
- 失敗した支払い済み注文は `retry` になり、次の注文に充てられる（払い直し不要）。
- 管理者でログインしたブラウザは全メニュー無料（`admin_free`）。

### メニューと料金の考え方（料金は `PRICES`、管理画面で変更可）
- アイテム（数量指定）：1種類ごと `other`（標準25円）
- キャラ指定：1体・1項目ごと `chara`（50円）
- サブ（章クリアなど）：1件ごと `other`
- レジェンド：段階が上がるごとに `other` 1件分（`LEGEND_TIERS`）。真レジェンドは旧レジェンド冠1が前提
- イベステ：1マップごと `other`。注文の中では `others` に `EV:<マップID>` として入る。前提（`reqs`）を満たさないと選べない
- クラッシュ解除 `crash`、プレイ時間 10分ごと `playtime10`、バン保証 `ban_guarantee`、複製 `clone`、初期垢作成 `restart`
- クーポン：200円以上の注文完了で10%引き（`NK-`、30日）。管理者発行の無料（`FR-`、1回だけ0円）

### ツムツム代行（もう1つの大メニュー）
- 画面は `/tsum`（`templates/tsum.html`）。トップページ（`/`）は**最初に大メニュー（にゃんこ／ツムツム）**を出し、にゃんこを選ぶといつもの画面、ツムツムを選ぶと `/tsum` へ行く。`/?game=nyanko`（または `#nyanko`）で来たときは大メニューを飛ばす。実行中・完了の注文を自動で開くときも閉じる。
- 注文・支払い・実行枠・クーポンは**にゃんこと共通**（`_place_order`・`/api/orders/<id>/pay`・`_slots`）。実行だけ `tsum_service.py` に分かれる。
- 料金はメニュー1件ごと（`PRICES` の `tsum_*`。管理画面で変更可）。数量では変わらない。
- 引き継ぎ番号で受け取ったアカウントは、**失敗しても必ず新しい引き継ぎ情報を発行して返す**（返さないとアカウントに戻れない）。
- LINE の画像認証は、実行中のプロセス → 画面 → `POST /api/jobs/<id>/captcha` → 実行中のプロセス の順で受け渡す（実行用のパイプは双方向にした）。
- アカウント情報（メールアドレス・パスワード・引き継ぎ番号）は、画面・ログ・注文の要約・Actions のログに出さない。注文ファイル（`orders.enc`・暗号化）にだけ入る。
- くわしくは `docs/TSUM.md`。

### 古い注文のための互換（消さないこと）
支払い済みの古い注文が残っていることがある。メニュー名やデータの形を変えるときは、**古い名前でも実行できるように残す**（例：`engine.py` の旧レジェンド名、`EVENT_BY_ID` のコラボ分、`ITEMS` から外した「1億DL記念チケット」も engine 側は対応したまま）。
安全モード・試運転は廃止済み（送られても無視する）。

---

## 4. 本番への反映（デプロイ）

| 変えたもの | どうなるか |
|---|---|
| お店のファイル（`app.py` など） | `main` に push → **`vps-deploy.yml`** が自動で反映。実行中・決済中の注文が終わるまで最大12分待ってから入れ替える |
| `*.md`・`docs/**`・`xbot/**`・`exchange/**`・`.github/workflows/xbot-*.yml` など | お店のデプロイは動かない |
| xbot | **`xbot/deploy/run.txt` を書き換えて `main` に push したときだけ** `xbot-deploy.yml` が動く |

`xbot/deploy/run.txt`（1行目が手順。同じ内容だと変化しないので、最後の行の `#番号` を変えて使う）：

| 1行目 | 何をするか |
|---|---|
| `install` | xbot を入れる・更新する（サービス再起動） |
| `status` | 状態とログ（DM の相手名は伏せ字） |
| `probe` | DM 画面の作りを調べる（読むだけ） |
| `dmcheck` | 下書きモードのときだけ DM 確認を1回 |
| `caddy`（2行目にドメイン） | 管理サイトの URL を Caddy に追加（reload のみ） |
| `lab`（2行目＝件数、3行目＝方法） | `xbot/lab` の実験。動いている xbot は止めず、プロフィールの写しで動かして写しは消す |

その他のワークフロー：`ci.yml`（全テスト）、`parallel-regression.yml`、`game-data-check.yml`、`auto-update.yml`（毎週 bcsfe の新しい版を確認し、テストが通れば `main` の `requirements.txt` を更新）、`vps-diag.yml`（注文状態の調査・読むだけ）、`xbot-survey.yml`（VPS の状態・読むだけ）。`fly-deploy.yml`・`diag.yml` は旧 Fly.io 用（手動のみ）。

**反映後の確認**：`curl https://nyanko-daiko-tnshop.duckdns.org/health`（200）、`/api/state`（`running_jobs`・`paying_orders` など）。xbot は `run.txt` の `status`。GitHub Actions のログは公開なので、ログに個人情報を出さない。

**GitHub Secrets（値はチャットに貼らせない・コードに書かない）**：`VPS_HOST`、`VPS_PASSWORD`、`ADMIN_PASSWORD`、`SITE_DOMAIN`、`XBOT_ADMIN_PASSWORD`（ほか旧 Fly 用 `FLY_API_TOKEN` など）。VPS へは Actions からパスワードで SSH する（`sshpass`）。

---

## 5. テスト（push の前に全部）

```sh
pip install -r requirements.txt            # flask, gunicorn, bcsfe, aiohttp, cryptography
for t in test_new_features test_order_flow test_hold_e2e test_improvements test_item_types \
         test_parallel test_paypay_hold test_paypay_gateway test_cat_customization \
         test_save_audit test_save_repair test_github_runtime_store test_security test_tsum; do
  python3 -m unittest $t || break
done
python3 selftest.py                         # 最後が「NG 0件」
python3 smoke_bcsfe.py                      # bcsfe の機能がそろっているか
python3 xbot/test_logic.py                  # xbot を触ったとき（X には接続しない）
python3 -m unittest test_ui_results         # 完了画面のコードの取り違えがないか（ブラウザのテスト。Chromium がなければ飛ばす）
python3 -m unittest test_price_parity       # 画面の金額とサーバーの金額が一致するか（ブラウザのテスト・60通り）
ruff check .                                # 書き方の検査（ruff.toml）
```

- 料金・注文・支払い・クーポンを変えたら、**先にテストを足す**（`test_new_features.py` などに例が多い）。
- 画面（`templates/`）や料金（`pricing.py`）を変えたら、ローカルで起動してブラウザで確認し、**画面の金額とサーバーの金額が一致する**ことを確かめる。決済なしで動かすなら `PAYMENTS_ENABLED=0`。
- PayPay や X への通信はテストではすべてモック。

---

## 6. 守ること

1. **利用者がいる**。決済と注文の流れを壊さない。
2. **公開リポジトリ**。パスワード・トークン・利用者の情報・DM の相手名・X/PayPay の ID・引継ぎコードを、コミットにもログにも出さない。Actions のログで出すときは伏せ字にする（`status` の `sed` を参考に）。
3. **料金はコードで変えない**（管理画面の `PRICES`／`prices.json`）。
4. **古い注文の互換を残す**（§3）。
5. **本番の X で投稿・DM 送信をしない**（依頼主の確認があれば別）。xbot には下書きモード（`dry_run`）がある。
6. 動いているサービスを止める・再起動するとき、既存のファイルを消すときは、先に依頼主に確認する。
7. エラーが出たら原因と対処案を伝える。無理に回避しない。
8. X や Cloudflare の防御を、Cookie の細工などでわざとすり抜けない（§7）。

---

## 7. xbot の注意

- **X ログイン**：パスワードは使わない・聞かない。依頼主が iPhone で取った Cookie（`auth_token`・`ct0`、または Cookie-Editor の JSON）を管理サイトに貼る。
- **ブラウザ**：投稿は iPhone と同じ WebKit、DM は Chromium（iPhone 側の Cookie を写す `DMBrowser`）。通信は Cloudflare WARP 経由（xbot だけ）。
- **画面の目印**（`data-testid`）は X の都合で変わる。DM（XChat）は Shadow DOM の中。新しい端末では4桁のチャット暗証番号が要る。
- **同じプロフィールを2つのブラウザで同時に開かない**（`PROFILE_LOCK`）。Playwright の同期 API は同じスレッドで入れ子にできない。
- **DM の失敗で予約投稿を止めない**。以前、DM のタイムアウトが一晩続いて投稿まで止めた。
- **ふつうの会話が0件になる原因（判明済み）**：リクエストの「その他」を開くと Cloudflare にボットと判定され、そのブラウザからは X の全ページが 403 になる。直し方（会話 → リクエスト → その他 を別ブラウザで、「その他」は1時間に1回）はブランチ `claude/xbot-next` にあり、**本番未反映**。詳しくは `docs/XBOT_PLAN.md`。

---

## 8. ブランチ

| ブランチ | 役割 |
|---|---|
| `main` | 本番。push で反映される |
| `claude/zealous-ritchie-5o2epn` | これまでの作業ブランチ（`main` とほぼ同じ。換金 BOT のコミットは入っていない） |
| `claude/xbot-next` | xbot の改善（投稿優先・失敗時の自動ブレーキ・要対応ルール・DM の直し方）。テスト済み・本番未反映 |

- 大きな変更は別ブランチで作ってテストしてから `main` へ。
- `main` に入れるときは、最新の `origin/main` に載せる（`git fetch` してから。ほかの人が `main` を更新していることがある）。
- PR は依頼されたときだけ作る。

---

## 9. 作業の進め方（コツ）

- 依頼主は結論を先に知りたい。終わったら「何が変わったか・確かめたこと・残っていること」を短く日本語で伝える。
- 本番の反映は Actions の結果（成功／失敗）と `/health` で確かめてから「できた」と言う。
- 本番の状態を知りたいときは、まず読むだけのもの（`/api/state`、`vps-diag.yml`、`run.txt` の `status`）を使う。
- 大きな変更をしたら `docs/BETA_FLY_PAYPAY.md`（お店）や `docs/XBOT_NOTES.md`（xbot）に1行残す。
