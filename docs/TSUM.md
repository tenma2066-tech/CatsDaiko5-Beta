# ツムツム代行（お店のもう1つの大メニュー）

「LINE：ディズニー ツムツム」の代行。にゃんこ代行と同じお店の中にあり、**画面だけ別**（`/tsum`）。
注文・PayPay の支払い・実行枠（4並列）・クーポン・管理画面の料金設定は、にゃんこ代行と**同じ仕組みを共有**している。

---

## 1. ファイル

| ファイル | 中身 |
|---|---|
| `tsum_engine/`（6ファイル） | 依頼主の Discord 版から持ってきた**素のスクリプト**。ゲームとの実際のやり取り（LINE ログイン・ゲーム API・ガチャ・引き継ぎ）。**中身は変えない** |
| `tsum_engine/tsum_core.py` | Discord 版 `tsum_bot.py` から、Discord に関係ない部分（`forge_sync` など）を**1文字も変えずに抜き出した**もの。抜き出した行の範囲はファイル先頭の `EXTRACTED_FROM` に書いてある。上半分（prelude）だけが Web 版用の土台（設定・ログ・プロキシ） |
| `tsum_service.py` | お店から呼ぶ入口。メニュー表・入力検査・1件の実行（ログイン → メニューを順に実行 → 引き継ぎ情報の発行） |
| `templates/tsum.html` | 利用者の画面（メニュー・アカウント・支払い・状況・画像認証・結果） |
| `pricing.py` の `compute_tsum_amount` | 金額（サーバー側の正） |
| `app.py` | `/tsum`（画面）、`/run` のツムツム分、`/api/jobs/<id>/captcha`、ワーカーの `_run_tsum_job` |
| `test_tsum.py` | テスト（ゲーム・LINE への通信はすべてモック） |

実行時に書き込む場所は `TSUM_DATA_DIR`（本番は `/data/tsum`、未設定なら `instance/tsum`）。
起動時に `tsum_engine/` の中身をそこへ写してから読み込むので、**リポジトリの中には何も書き込まない**。
写した先に置かれるもの：`appver.json`（ゲームのバージョンのキャッシュ）、`tsum_guest_transfers.json`（発行した引き継ぎ情報の控え）、`tsum_guest_orphans.json`（発行に失敗したセッション）、`tsum_debug.log`（診断用。パスワードは文字数だけで、値は残らない）。

## 2. メニューと料金

料金は**管理画面**（`/admin`）で変える。コードでは変えない。既定値は Discord 版と同じ。

| キー | メニュー | 数量 | 既定 |
|---|---|---|---|
| `tsum_coin` | コイン指定 | 0〜2億 | 100円 |
| `tsum_coin_max` | コインMAX | — | 100円 |
| `tsum_tsum_lv` | ツムレベルMAX（セット中のマイツム） | — | 100円 |
| `tsum_box` | セレクトBOX完売 | — | 200円 |
| `tsum_premium` | プレミアム完売 | — | 200円 |
| `tsum_score` | スコア指定 | 0〜2,147,483,647 | 100円 |
| `tsum_score_max` | スコアMAX | — | 100円 |
| `tsum_medal` | メダル指定 | 1〜450,000（安全上限） | 100円 |
| `tsum_level_max` | プレイヤーレベルMAX | — | 100円 |
| `tsum_guest_create` | ゲストアカウント生成（単独でのみ注文可） | — | 50円 |

- 料金は**メニュー1件ごと**。数量では変わらない。
- 複数選ぶと、**コイン → ツムレベル → ガチャ → スコア → メダル → レベル** の順に、1回のログインで続けて実行する（ガチャはコインを使うので後ろ）。
- Discord 版の `guest_create` は0円だったが、料金欄は1円以上しか入れられないので50円にしている。無料で出すときは**無料クーポン**（`FR-`）を使う。

## 3. アカウントの入れ方（3通り）

| 画面の選択 | 中で起きること | 返すもの |
|---|---|---|
| メールアドレスでログイン | LINE の ID/パスワードでログイン（`headless_session_direct`）。**画像認証や PIN が出ることがある** | なし（アカウントは利用者のまま） |
| 引き継ぎ番号で引き継ぐ | ゲームの引き継ぎでアカウントを**こちらの端末に移す**（`guest_takeover`） | **新しい引き継ぎ番号とパスワード**（必ず発行する） |
| 新しいゲストアカウントを作る | `guest_create` で新規作成 | 新しい引き継ぎ番号とパスワード |

**引き継ぎでアカウントが移ったあとは、代行が失敗しても必ず新しい引き継ぎ情報を発行して返す**（返さないとアカウントに戻れなくなる）。発行できなかったときは控え（`tsum_guest_orphans.json`）に残し、注文は「結果不明」として管理者に回す。

### 画像認証（CAPTCHA）
LINE が画像認証を出したら、実行中のプロセスが画像をジョブに載せ、画面に出す（`/status/<job>` の `captcha`）。
利用者が入力した文字は `POST /api/jobs/<job>/captcha` → 親プロセス → パイプ → 実行中のプロセスへ渡る。待ち時間は4分（`TSUM_CAPTCHA_WAIT`）。
このために実行用のパイプを双方向にした（にゃんこ代行では親から何も送らないので動きは同じ）。

## 4. 設定（環境変数）

| 変数 | 既定 | 何か |
|---|---|---|
| `TSUM_DATA_DIR` | `/data/tsum`（無ければ `instance/tsum`） | 作業用のフォルダ |
| `TSUM_SOCKS_URL` | なし | ログインを通す SOCKS プロキシ（Tor など）。未設定なら直接つなぐ |
| `TSUM_TOR_GAME_API` | `0` | `1` にするとゲーム本体の通信もプロキシ経由（1件で数百〜数千回叩くので遅くなる） |
| `TSUM_AUTO_FILL_COINS` | なし | `1` にすると、ガチャ・レベル上げの途中でコインが足りなくなったとき自動で補充する（コインは有料メニューなので既定は補充しない） |
| `TSUM_SELECT_BOX_ID` / `TSUM_PREMIUM_GACHA_ID` | `12006012` / `12006007` | ガチャIDの予備。通常は開催中のIDをゲームから取り直す |

Discord 版は Windows で Tor を立てて IP を変えていた。Web 版は VPS の固定IPから直接つなぐのが既定。

## 5. テスト

```sh
python3 -m unittest test_tsum              # メニュー・料金・実行・注文・画像認証（通信はすべてモック）
python3 -m unittest test_price_parity      # 画面とサーバーの金額の一致（にゃんこ＋ツムツム・ブラウザ）
```

ゲーム・LINE・PayPay への実際の通信はテストしない（できない）。`tsum_engine/` を差し替えたら、**依頼主の環境で1件だけ実際に流して確かめる**こと。

## 6. `tsum_engine/` を更新するとき

1. Discord 版の新しいファイルを `tsum_engine/` に上書きする（`tsum_core.py` は `EXTRACTED_FROM` の行範囲を同じ方法で抜き出し直す）。
2. `tsum_service.py` が使っている名前が残っているか確かめる：`forge_sync`・`MAX_PRESETS`・`describe_login_failure`・`tsum_guest.guest_takeover`・`guest_create`・`guest_issue_transfer`・`mark_pending_done`・`Forge`・`_proxies_dict`・`_game_api_proxy`・`_unique_game_proxy`・`_save_guest_transfer`・`_save_orphan_guest`。
3. `python3 -m unittest test_tsum` を通す（モックの形が合わなければ、そこが変わったところ）。
4. 本番で1件流して確かめる。
