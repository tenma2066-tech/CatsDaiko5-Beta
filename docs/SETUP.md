# 導入ガイド（自分のお店として動かす）

このソース一式を、自分のお店として公開するまでの手順です。上から順に進めてください。
仕組みの全体像は `README.md`、注文と支払いの決まりは `docs/REBUILD_FOR_OTHERS.md` にあります。

## 0. 先に用意するもの

| もの | 何に使うか |
|---|---|
| GitHub のアカウントと**非公開**リポジトリ | ソースを置く。push するとサーバーへ自動で配置される |
| VPS（Ubuntu 24.04・メモリ2GB以上） | お店を動かすサーバー。22・80・443 番を開ける |
| PayPay のアカウント（受け取り用） | お客さんの送金リンクを受け取る。SMS が届く電話番号が必要 |
| （任意）独自ドメイン | 無ければ `<IP>.sslip.io` で自動的に HTTPS になる |
| （任意）Anthropic の API キー | Q&A チャットを AI に答えさせる。無ければ固定の案内文 |

**運営するのはあなた自身です。** 各ゲーム・PayPay の利用規約、特定商取引法の表示などは、ご自身で確認してください。
PayPay は公式の加盟店 API ではなく、アプリと同じ通信を使います。仕様が変わると動かなくなることがあります。

## 1. 手元で動かしてみる（支払いなし）

```bash
pip install -r requirements.txt -c constraints.txt
PAYMENTS_ENABLED=0 ADMIN_PASSWORD=test python app.py
```

`http://localhost:10000` を開くと、トップと各お店が見られます。`PAYMENTS_ENABLED=0` のあいだは支払いを飛ばして実行します。

## 2. お店の名前と問い合わせ先を決める

コードは書き換えず、環境変数で設定します（`site_config.py` が読みます）。

| 環境変数 | 例 | 内容 |
|---|---|---|
| `SHOP_NAME` | `MY SHOP` | 店名（画面のタイトル・フッター・Q&A の名乗り） |
| `SHOP_X_ID` | `your_x_id` | 問い合わせ先の X のID（@ なし）。空なら問い合わせ先を出さない |
| `REVIEW_POST_ID` | `1234567890` | 以前 X で感想を集めていたポストのID。実績ページの「今までの実績を見る」のリンク先になる。空なら出さない |
| `REVIEWS_ENABLED` | `1` | 実績（完了後に星と感想を書いてもらう）を使うか。`0` で入力のお願いも実績ページも出さない |
| `GOOGLE_SITE_VERIFICATION` | （Search Console の値） | 検索の所有確認。空なら出さない |

画像（`static/game-*.jpg` `static/ogp-*.jpg` `static/icon-*.png`）は、自分のお店のものに差し替えてください。

「ご利用の流れ」の図解（`static/flow.jpg`・`static/flow.webp`＝にゃんこ用、`static/tsum-flow.jpg`・`static/tsum-flow.webp`＝ツムツム用）は
入っていません（店名と連絡先を描き込む画像のため）。自分のお店用に作って置くと、各画面に「ご利用の流れ（図解）」が出ます。
リンクを貼ったときに出る画像（`static/ogp-nyanko.jpg`・`static/ogp-tsum.jpg`、1200×630）も同じ理由で入っていません。
置かなければ、店名の入っていないゲームの絵（`static/game-*.jpg`）が代わりに使われます。

## 3. サーバーに置く（GitHub Actions で自動）

リポジトリの Settings → Secrets and variables → Actions に、次を登録します。

| 名前 | 必須 | 内容 |
|---|---|---|
| `VPS_HOST` | ◯ | サーバーの IP アドレス |
| `VPS_PASSWORD` または `VPS_SSH_KEY` | ◯ | root のパスワード、または秘密鍵 |
| `ADMIN_PASSWORD` | ◯ | 管理画面（`/admin`）のパスコード。長く、使い回さない |
| `SITE_DOMAIN` | 任意 | 独自ドメイン |
| `ANTHROPIC_API_KEY` | 任意 | Q&A チャットの AI |

`main` に push すると `.github/workflows/vps-deploy.yml` が動き、Docker と Caddy（HTTPS）を入れて起動します。
暗号鍵（`SECRET_KEY`・`PAYPAY_ENCRYPTION_KEY`）は**初回だけ**サーバーで自動生成され、`/opt/catsdaiko/.env` に入ります。
店名などの環境変数（手順2）も、この `.env` に 1行ずつ書き足して `docker compose up -d` し直せば反映されます。

> **`.env` の `PAYPAY_ENCRYPTION_KEY` は必ず控えてください。** 失うと、保存した PayPay の接続情報と注文が読めなくなります。

## 4. PayPay をつなぐ

1. `https://<あなたのサイト>/admin` を開き、パスコードを入れる。
2. 「PayPay」タブで電話番号とパスワードを入れる → 届いた SMS の番号を入れる。
3. 「接続済み」になれば、お客さんの送金リンクを受け取れます。

最初に、自分で一番安いメニューを1件注文して、支払いから結果の表示まで通ることを確かめてください。

## 5. 料金を決める

管理画面の「設定」タブで、すべてのメニューの料金を変えられます（保存するとすぐ新しい注文から反映）。
チケット・支援BOX・ソースコード販売を使わないなら、トップ（`templates/home.html`）からそのカードを消してください。

## 6. 日々の運用

- **保留**：PayPay 側の安全確認で止まった送金は、90秒ごとに自動で再確認します。管理画面の「注文」で手動の再確認もできます。
- **要確認（dead）**：自動で確定できなかった注文。PayPay アプリで着金を確かめてから「支払い済みにする」を押します。
- **メンテ表示**：管理画面の「設定」でオンにすると、新しい注文と支払いだけ止まります（支払い済みの注文は進められます）。
- **実績**：完了したお客さんが星と感想を書きます（1注文1回）。不適切なものは管理画面の「実績」タブで非表示にできます。
- **バックアップ**：管理画面からダウンロードできます（暗号鍵は入りません。`.env` は別に控える）。
- **更新**：`main` に push するたび、実行中の注文が終わるのを待ってから入れ替わります。

## 環境変数の一覧

| 名前 | 既定 | 内容 |
|---|---|---|
| `SECRET_KEY` | （自動生成） | ログイン状態（Cookie）の署名 |
| `PAYPAY_ENCRYPTION_KEY` | （自動生成） | PayPay の接続情報・注文の暗号化 |
| `ADMIN_PASSWORD` / `ADMIN_PASSWORD_FILE` | — | 管理画面のパスコード（ファイルで渡す方が安全） |
| `PAYMENTS_ENABLED` | `1` | `0` で支払いなし（開発用） |
| `SITE_URL` | 自動 | 公開URL（検索・リンクプレビュー用） |
| `PAYPAY_DATA_DIR` `PAYPAY_LEDGER_PATH` | `instance/paypay` | 注文・台帳・料金・設定の保存先（消えない場所に） |
| `TSUM_DATA_DIR` `PUNI_DATA_DIR` | — | ツムツム・ぷにぷにの作業用フォルダ |
| `MAX_CONCURRENT_JOBS` | `4` | 同時に実行する件数 |
| `LONG_JOB_SLOTS` | `3` | ぷにぷにの長時間周回（回し放題・50周以上）専用の実行枠。1件あたりメモリ約70MB。`0` でふつうの枠を使う |
| `ANTHROPIC_API_KEY` `QA_MODEL` | — | Q&A チャット（`docs/QA_SETUP.md`） |
| `TSUM_SOCKS_URL` `PUNI_SOCKS_URL` | — | ログイン時に使うプロキシ（`docs/TSUM.md`・`docs/PUNI.md`） |
| `SHOP_NAME` `SHOP_X_ID` `REVIEW_POST_ID` `REVIEWS_ENABLED` `GOOGLE_SITE_VERIFICATION` | — | お店の名前など（手順2） |

## うまくいかないとき

| 症状 | 見るところ |
|---|---|
| デプロイが緑なのに何も起きない | `VPS_HOST` が未登録だと「何もせず成功」になります |
| サイトが開かない | VPS のパケットフィルターで 80・443 が開いているか |
| 送金リンクが全部エラー | 管理画面の PayPay が「接続済み」か。「接続を更新」を押す |
| 実行が始まらない | 管理画面のダッシュボードで実行中・順番待ちの件数を見る |
| Q&A が固定文しか返さない | `ANTHROPIC_API_KEY` が `.env` に入っているか |

## 守りの仕組み（v1.21）

| 仕組み | 中身 | 場所 |
|---|---|---|
| 中身のセキュリティ方針（CSP） | 自分のサイトのファイルと、応答ごとに変わる合い言葉（nonce）の付いたスクリプトだけを動かす。ほかのサイトのスクリプト・埋め込み・送信先は止まる | `app.py` の `_private_response` / `_csp_nonce` / `_template` |
| 保存させない | 画面と API の応答は `Cache-Control: no-store`（結果のコードが端末やブラウザの控えに残らない） | `_private_response` |
| コメントを配らない | 配る HTML・JavaScript・CSS から説明のコメントを取り除く（元のファイルはそのまま） | `strip_comments` / `_static` |
| のぞき見よけ | 右クリック・Ctrl+S・Ctrl+U・F12 を止める（気軽にのぞかれるのを減らすだけ。本当の守りは上の3つとサーバー側） | `static/guard.js` |
| 管理画面の2段階認証 | 認証アプリの6桁（管理画面 → 設定 → 2段階認証で有効にする）。スマホをなくしたら、データフォルダーの `admin_2fa.json` を消すとパスコードだけに戻る | `admin_2fa.py` |
| 自動ログアウト | 管理画面は、60分さわらないとログアウト（ログインからは最長12時間） | `_is_admin` |

HTML に `onclick="…"` を直接書くと CSP で動かない。ボタンの確認は `data-confirm="…"`（管理画面）か、スクリプト側の `addEventListener` で書く。

## 意見・質問箱（v1.21）

トップの7つ目のメニュー（`/box`）。だれでも書けて、読めるのは管理者だけ（管理画面 →「意見箱」）。
保存先は台帳の `feedback` 表。暴言は実績と同じ検査で断る（URL・連絡先は書ける）。自動投稿よけに、見えない欄と回数の上限がある。
テスト：`test_v121.py`。

## 購入時の人間確認（Cloudflare Turnstile・v1.22）

支払い（送金リンクを送る）の直前と、意見・質問箱の送信に、Cloudflare の「人間です」のチェックを出す。自動の大量注文・リンクの総当たりを止める。

| 設定 | 中身 |
|---|---|
| `TURNSTILE_SITE_KEY` | Cloudflare の Turnstile で作ったウィジェットの「サイトキー」（画面に出る） |
| `TURNSTILE_SECRET_KEY` | 同じウィジェットの「シークレットキー」（サーバーだけが持つ） |

- 2つともあるときだけ有効。無ければ、確認なしで今までどおり動く。GitHub の Secrets に入れておくと、デプロイのときに `.env` へ書かれる。
- 画面のチェックを飛ばしても、サーバーが Cloudflare に確かめる（`human.py` の `verify`、`app.py` の `_human_ok`）。
- 一度通ったブラウザには15分は聞き直さない。保留の確認・支払い済みの再開・管理者の無料実行には出さない。
- Cloudflare に問い合わせられないとき（障害）は、確認なしで通す（売り上げを止めない）。
- ドメインを変えたら、Cloudflare のウィジェットの「ホスト名」に新しいドメインを足す。
- テスト：`test_human.py`。
