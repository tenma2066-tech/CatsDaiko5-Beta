# TN SHOP（ゲーム代行ショップ）

スマホのブラウザだけで注文から受け取りまで終わる、ゲーム代行のお店です。
お客さんがメニューを選び、PayPay の送金リンクで支払うと、サーバーが入金を確認して自動で実行し、結果を画面に出します。

## お店にあるもの

| 入口 | 内容 | 画面 | 実行するところ |
|---|---|---|---|
| `/nyanko` | にゃんこ大戦争（セーブを編集して新しい引継ぎコードを発行） | `templates/main.html` | `engine.py`（bcsfe） |
| `/tsum` | ツムツム（ゲームサーバーに直接ログイン） | `templates/tsum.html` | `tsum_service.py` → `tsum_engine/` |
| `/puni` | ぷにぷに（同上） | `templates/puni.html` | `puni_service.py` → `puni_engine/` |
| `/ticket` | チケット購入（1回無料・期間無料。クーポンコード式） | `templates/extra.html` | コードを発行するだけ |
| `/support` | 支援BOX（お礼に30%引きクーポン） | `templates/extra.html` | 同上 |
| `/source` | ソースコード購入（この仕組み一式の zip） | `templates/extra.html` | `source_pack.py` |
| `/reviews` | 実績（完了したお客さんの星5段階の評価と感想） | `templates/reviews.html` | 入力欄は `static/review.js` |
| `/dya/` | ミニゲーム（ダイナマイト野球・暗証番号つき。VPS だけ） | dya-proxy のランチャー | 別コンテナ `dya`（`docs/VPS_DEPLOY.md`） |
| `/osero/` | ミニゲーム（UltraOSERO・6人まで。VPS だけ） | `osero/` | 別コンテナ `osero` |
| `/hunt/` | ミニゲーム（彩狩・ひとりでも協力でも。VPS だけ） | `hunt/` | 別コンテナ `hunt`。カードは `HUNT_ENABLED` |
| `/chinchiro/` | ミニゲーム（チンチロ・1ファイル・通信なし） | `chinchiro/index.html` | お店のアプリがそのまま返す（専用の CSP）。`CHINCHIRO_ENABLED=0` で隠す |
| `/admin` | 管理画面（PayPay 接続・注文・料金・クーポン・メンテ表示） | `templates/admin.html` | — |

右下の「質問する」は AI の Q&A チャット（`qa_bot.py`）。鍵（`ANTHROPIC_API_KEY`）が無ければ固定の案内文になります。

## 仕組み（1つの注文がたどる道）

```
メニューを選ぶ → 注文を作る（金額はサーバーが計算）→ PayPay 送金リンクを貼る
   → 入金を確認（保留なら90秒ごとに自動で再確認）→ 実行（同時4件まで・それ以上は順番待ち）
   → 結果を表示（完了から3日は注文番号で呼び戻せる）
```

注文の状態は `unpaid → paying → (hold) → paid → starting → started → done`。自動で確定できないものは `dead`（管理画面で人が見る）。
この流れは全部門で共通で、`app.py` の「注文」の節にあります。くわしい決まりは `docs/REBUILD_FOR_OTHERS.md`。

## ファイルの地図

| ファイル | 役割 |
|---|---|
| `app.py` | サーバー本体。画面・注文・支払い・実行枠・管理画面。先頭に目次があります |
| `site_config.py` | 店名・問い合わせ先など、お店ごとに変わる値（環境変数で上書き） |
| `pricing.py` | 料金の計算。料金そのものは管理画面で変えられる（`PRICES`） |
| `paypay_gateway.py` | PayPay とのやり取り・台帳（SQLite）・クーポン・暗号化保存 |
| `engine.py` `save_audit.py` `save_repair.py` | にゃんこのセーブ編集・検査・修復 |
| `tsum_service.py` `puni_service.py` | ツムツム・ぷにぷにのメニュー定義と実行 |
| `qa_bot.py` | Q&A チャット（Claude API） |
| `source_pack.py` | 販売用のソース一式（zip）を作る |
| `templates/` `static/` | 画面（HTML・CSS・JavaScript）と画像 |
| `deploy/vps/` `.github/workflows/` | VPS（Docker + Caddy）への自動デプロイ |
| `test_*.py` `selftest.py` | テスト（実際の PayPay・ゲームには接続しない） |

## 動かす

手元で試す（支払いなし）:

```bash
pip install -r requirements.txt -c constraints.txt
PAYMENTS_ENABLED=0 python app.py        # http://localhost:10000
```

本番に置く手順・必要な設定は **`docs/SETUP.md`**（はじめての人向け）と `docs/VPS_DEPLOY.md`。

## テスト

```bash
python -m unittest test_order_flow test_tickets test_qa     # 一部だけ
for t in test_*.py; do python -m unittest "${t%.py}"; done   # 全部
ruff check .
```

## もっと知る

- 新しい部門を足す：`docs/DEPARTMENTS.md`
- チケット・支援BOX・ソースコード販売：`docs/TICKETS.md`
- Q&A チャットの設定：`docs/QA_SETUP.md`
- ツムツム・ぷにぷにの中身：`docs/TSUM.md`・`docs/PUNI.md`
- 保守のルール：`MAINTENANCE.md`
