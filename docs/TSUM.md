# ツムツム代行（お店のもう1つの大メニュー）

「LINE：ディズニー ツムツム」の代行。にゃんこ代行と同じお店の中で動く。ページは、トップ `/`（大メニュー）・にゃんこ `/nyanko`・ツムツム `/tsum` の3つに分かれている。
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

料金は**管理画面**（`/admin`）で変える。コードでは変えない。既定値は Discord 版の半額。

| キー | メニュー | 数量 | 既定 |
|---|---|---|---|
| `tsum_coin` | コイン指定 | 0〜2億 | 50円 |
| `tsum_coin_max` | コインMAX | — | 50円 |
| `tsum_tsum_lv` | ツムレベルMAX（セット中のマイツム） | — | 50円 |
| `tsum_box` | セレクトBOX完売 | — | 100円 |
| `tsum_premium` | プレミアム完売 | — | 100円 |
| `tsum_premium_plus` | プレミアムBOX+（メダルで引けるところまで。支払い方法の番号は最初の実行で、ルビー0個のアカウントで単発を引いて確かめ、保存する） | — | 100円 |
| `tsum_score` | スコア指定 | 0〜2,147,483,647 | 50円 |
| `tsum_score_max` | スコアMAX | — | 50円 |
| `tsum_medal` | メダル指定 | 1〜100,000,000（上限1億） | 50円 |
| `tsum_level_max` | プレイヤーレベルMAX | — | 50円 |
| `tsum_guest_create` | ゲストアカウント生成（単独でのみ注文可） | — | 30円 |

- 料金は**メニュー1件ごと**。数量では変わらない。
- 複数選ぶと、**コイン → ツムレベル → ガチャ → スコア → メダル → レベル** の順に、1回のログインで続けて実行する（ガチャはコインを使うので後ろ）。
- Discord 版の `guest_create` は0円だったが、料金欄は1円以上しか入れられないので30円にしている。無料で出すときは**無料クーポン**（`FR-`）を使う。

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
画像ごとに `id` を付け、答えにも同じ `id` を付けて送る。時間切れのあとに届いた古い答えは、実行プロセスが次の画像を出す前に捨てる（取り違え防止）。

ログイン失敗の文言は `tsum_core.describe_login_failure(reason)` が、実際の失敗理由（`line_password_login.py` が返す生の文字列）から決める。画像認証まわりは「画像の取得に失敗」「文字が入力されなかった」「文字が違う／再要求された」「reCAPTCHA 要求」を別々に出す。確認できない「LINE 側の一時制限」「20〜30分」などの推測は書かない（2026-11-23 修正）。テストは `test_tsum.py` の `LoginFailureMessages`。

#### 画像認証が「繰り返し出る」ときの根本原因と対処（2026-11-23）
LINE はデータセンター／VPS／クラウドの固定IP（Fly・さくら等）からのログインを怪しみ、**画像認証を毎回出す／送信後に reCAPTCHA（Google・打鍵不可）へ切り替える**ことがある。送信元IPが固定だと、利用者が正しく入力しても LINE が再要求し続け、ログインが完了しない。これが代行利用者側の「画像認証が繰り返し出てできない」の正体。

- `tsum_service._login_line` は、画像認証の再要求・reCAPTCHA を**別IPに変えられるときだけ**（SOCKS プロキシ設定時）別回線でやり直す。固定IPのときは叩き直さず（同じIPで繰り返してもアカウントに負荷をかけるだけ）、利用者には「サーバー側のアクセス状況が原因の可能性」を正直に伝える（`_login_failure_message`）。「VPN を変えて」「20〜30分待って」のような、利用者に実行できない・根拠のない案内は出さない。テストは `test_tsum.py` の `test_captcha_block_without_ip_rotation_is_not_retried` ／ `test_captcha_block_with_ip_rotation_is_retried_on_fresh_circuit`。
- **恒久対処（運用側）**：LINE ログインを住宅用／モバイル回線の**ローテーション型プロキシ**（または出口を変えられる Tor）に通す（`TSUM_SOCKS_URL`）。固定のデータセンターIPのままでは、コード側で何をしても画像認証・reCAPTCHA は減らない。プロキシを設定すると `_unique_game_proxy()` がやり直しごとに別の出口（別IP）を割り当て、上の自動リトライが効くようになる。
- **本番(VPS)での実装＝ホスト側 Tor（2026-11-23）**：`deploy/vps/remote-setup.sh` がデプロイのたびに、ホスト(Ubuntu)に `apt-get install tor` で Tor を入れ、`0.0.0.0:9050` で SOCKS を待ち受ける（SocksPolicy で private レンジ 127/8・10/8・172.16/12・192.168/16 のみ許可し他は拒否＝外部非公開。`docker.service` の後に起動）。app コンテナは `docker-compose.yml` の `extra_hosts: host.docker.internal:host-gateway` と `TSUM_SOCKS_URL=socks5h://host.docker.internal:9050` でそこへ繋ぐ。`IsolateSOCKSAuth` により、`fresh_socks_identity()` が付ける SOCKS の user:pass が違うと別の出口回線（別IP）になる。SOCKS を使うため `requests` の依存に **PySocks** を追加（これが無いと `socks5h://` は `InvalidSchema` で失敗する）。
- **なぜ `0.0.0.0` で待ち受けるか**：docker のホストゲートウェイIPは環境によって違う（このVPSは `192.168.0.1/20`。既定の `172.17.0.1` ではない）。特定IPに bind すると、そのIPが無い環境で tor の起動自体が失敗する（デプロイ #57 の原因）。`0.0.0.0` ならどのブリッジのゲートウェイからも届く。`host.docker.internal` はその環境の正しいゲートウェイに解決される。
- **なぜホスト側か**：ビルド中のコンテナ（`python:3.12-slim`＝Debian）からは `deb.debian.org` へ繋がらず `apt-get install tor` が `Connection timed out` で失敗する（デプロイ #55）。ホスト(Ubuntu)の apt は `archive.ubuntu.com` で動くので、イメージには足さずホストに入れる。デプロイログに `tor: active  listen=[0.0.0.0:9050]` と `tor reachable from app: OK`（app コンテナからの到達確認）が出れば有効。届かなければアプリは直接接続で動く（サービスは止まらない＝回線の回転だけ無効）。
- **効き目の注意**：LINE は Tor の出口IPも弾くことがあるので、画像認証/reCAPTCHA が止むかは実機ログインで要確認。弾かれる場合は `docker-compose.yml` の `TSUM_SOCKS_URL` を住宅/モバイルのローテーション型プロキシ（有料・契約が必要）に差し替える（ホスト Tor の節を無効化してよい）。

## 4. 設定（環境変数）

| 変数 | 既定 | 何か |
|---|---|---|
| `TSUM_DATA_DIR` | `/data/tsum`（無ければ `instance/tsum`） | 作業用のフォルダ |
| `TSUM_SOCKS_URL` | なし | ログインを通す SOCKS プロキシ（住宅/モバイルのローテーション型プロキシ・Tor の sidecar など）。未設定なら直接つなぐ。設定するとやり直しごとに別の出口IP（別回線）を使う。LINEログインのIPローテーション用 |
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

## 7. 画面のふるまい（`templates/tsum.html`）
- 実行中にタブを閉じても、開き直すと「このブラウザの注文」から状況・**引き継ぎ情報**を見直せる（`/api/orders/mine` の `tsum` の注文）。実行が始まった注文は自動で状況を開く。
- PayPay が保留のときは同じリンクを送り直させず、20秒ごとに注文の状態を見て、始まったら自動で状況を開く（サーバー側も90秒ごとに確認）。
- アップデート中（503 `updating`）は5秒おきに自動でやり直す。状況が30秒取れなくなったら止めて、注文一覧から拾い直す。
- 実行中はページを離れようとすると確認を出す。コピーは clipboard が使えない環境でも動く。
- 「← ゲームを選び直す」はトップ `/`（大メニュー）へのふつうのリンク。
