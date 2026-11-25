# 代行部門の追加ガイド（新しいゲームのShopを足すとき）

TN SHOP は現在3部門：**にゃんこ**（`/nyanko`）・**ツムツム**（`/tsum`）・**ぷにぷに**（`/puni`）。
注文・支払い（PayPay）・実行枠（同時4・待機20）・注文台帳・クーポン・履歴削除は**全部門で共通**。
4つ目を足すときは、**ぷにぷに（`puni_service.py` + `templates/puni.html`）を雛形**にすると速い。
ぷには「ゲームサーバに直接ログインして操作する」型。セーブ編集型（にゃんこ）とは別系統。

このファイルは、追加時に触る場所を1か所ずつ並べたチェックリスト。`<dept>` を新部門の短い識別子
（例：`mugen`）に読み替える。

---

## 0. 設計の型を選ぶ
- **ゲームAPI直結型**（ツム・ぷに）：ログイン→APIを叩いて改変。雛形は `tsum_service.py` / `puni_service.py`。
- **セーブ編集型**（にゃんこ）：引継ぎコードのセーブを編集して返す。`engine.py`。

以下は直結型（ぷにを雛形）の手順。

## 1. エンジン（配布コード）を置く
- `<dept>_engine/` を作り、依頼主の配布スクリプトを**未改変**で置く（`puni_engine/` と同じ）。
- 実行時に書くファイル（ログ・鍵・キャッシュ）は `<DEPT>_DATA_DIR`（既定 `/data/<dept>`、無ければ
  `instance/<dept>`）へ逃がす。リポジトリ内には書かない。`puni_service._prepare_engine` を複製
  （fcntl ロック＋原子コピー＋サイズ/mtime 判定。P-12/P-13 対策込み）。

## 2. サービス層 `<dept>_service.py`（`puni_service.py` を複製して改名）
必須の契約（app.py と画面がこの形を前提にする）：
- `MENUS`：`{key: {label, price, value?(min,max), unit?, desc, order}}`。`value` があれば数量入力。
- `SOLO_MENUS`：単独でしか選べないメニュー（アカウント作成系など）。
- `STAGE_MENUS` 等、追加の入力が要るメニュー（任意）。
- `MODES` / `MODE_LABELS`。
- `sort_menus(keys)` / `menu_label(key)` / `validate_order(...) -> (menus, qty, ..., error)` /
  `check_account(mode, id, pw) -> error文`。
- `pricing_units(menus, qty) -> [(price_key, 件数)]`（**画面の見積もりと一致させる要**）。
- `run_order(mode, login, password, menus, qty, ...) -> dict`。戻り値に **`delivered`**（1つでも
  提供したか＝自動再実行の可否・P-02）と、必要なら `account`/`moved` を含める。
- 通信は `_login_proxy()`（ログイン秘匿）と `_game_api_proxy()`（多量通信は既定直結、
  `<DEPT>_TOR_GAME_API=1` で経由）に分ける（ツムと同じ思想）。
- パスワード生成は `secrets`（P-14）、メール検証は厳しめ（P-17）。

## 3. 料金 `pricing.py`
- `PRICES` に `<dept>_*` キーを追加、`PRICE_LABELS` に対応ラベル。
- `compute_<dept>_amount(menus, qty)` を追加（`compute_puni_amount` を複製）。
- **件数計算は `<dept>_service.pricing_units` と画面JS `unitsForPrice` の3者を必ず一致**させる
  （ぷにで一度ここがズレて過少表示のバグ B-A が出た）。

## 4. app.py の結線（検索で見つかる既存の puni 分岐の隣に足す）
- `import <dept>_service`、`from pricing import ... compute_<dept>_amount`。
- `<DEPT>_TOTAL_STEPS = N`（進捗段階）。
- `_lock_subject`：`job.get("mode") == "<dept>"` の分岐（`"<dept>:" + login`）。
- `_run_<dept>_job(job, payload)`：`run_order` を呼び、`outcome_unknown` は
  `out["delivered"]`（提供済みなら自動再実行させない・P-02）で決める。
- `_worker_entry`：`job.get("mode") == "<dept>"` の分岐で `_run_<dept>_job`。
- 画面ルート `@app.get("/<dept>")` → `render_template_string(<DEPT>_HTML, ...)`。
- `/run` ディスパッチ：`if isinstance(data.get("<dept>"), dict): return _run_<dept>_order(...)`。
- `_run_<dept>_order` / `_new_<dept>_job` / `_<dept>_summary`（モードは**メニューから決める**。
  クライアントの mode を実行経路の判断に使わない・なりすまし対策 B-B）。
- `status()` の `total`：`{"tsum":..., "puni":..., "<dept>":<DEPT>_TOTAL_STEPS}.get(mode, TOTAL_STEPS)`（P-16）。
- `status()` の返却に `<dept>=job.get("<dept>")` を追加。
- `_public_order` に `"<dept>": (o.get("job") or {}).get("mode") == "<dept>"` を追加。
- `<DEPT>_HTML = _template("<dept>.html")`。
- `/sitemap.xml` の paths に `/<dept>`。

## 5. 画面 `templates/<dept>.html`（`templates/puni.html` を複製）
- デザインは**既存のトークンを踏襲**（`:root` の `--bg/--panel/--accent/...` と
  `@media (prefers-color-scheme:dark)`、角ゴシック、`.card/.btn/.menu/.bar/.pbar/.panel`）。
  現状トークンは各テンプレに同じ内容で複製されている（将来 `static/shop.css` に集約する余地）。
- 文字は簡潔に（1行主体）。注意・FAQ は折りたたみ（`<details class="card">`）。
- **履歴削除**：`loadResume` の各行に、`['done','dead','unpaid']` のとき `.btn.del`「削除」を付け、
  `forgetOrder(o,row)` → `POST /api/orders/<id>/forget`（共通エンドポイント）。
- **FAQ**：`#s-faq` の `.faq` を部門に合わせて文言調整。
- 画面の金額見積もり（`unitsForPrice`/`rows`）は**サーバの `compute_<dept>_amount` と一致**。
- `loadResume` の絞り込みは `o.<dept>` のみ（他部門の注文を混ぜない。にゃんこで
  `!o.tsum` だけに絞っていてぷにが混ざった回帰があったので注意）。

## 6. トップと導線
- `templates/home.html`：ゲームカードを1枚追加、「このブラウザの注文」に `mine<Dept>` 行、
  判定スクリプトの振り分けに `o.<dept>` を追加（にゃんこは `!o.tsum && !o.puni && ...`）。
- 画像 `static/game-<dept>.{webp,jpg}` と `static/ogp-<dept>.jpg`。

## 7. 設定・デプロイ
- `requirements.txt`：エンジンが要る依存だけ追記（重複は足さない）。
- `fly.toml [env]`：`<DEPT>_DATA_DIR = "/data/<dept>"`（ツム・ぷにと同じく明示）。
- 任意：`<DEPT>_SOCKS_URL`（送信元秘匿）・`<DEPT>_TOR_GAME_API`。

## 8. テスト `test_<dept>.py`（`test_puni.py` を複製）
- 入力検査・料金（`pricing_units`×`compute_*`×画面の一致）・状態遷移・部分提供
  （delivered）・なりすまし経路・異常系・app 結合（`/<dept>`・`/run`・`_run_<dept>_job` の
  `outcome_unknown`・`status` total・`forget`）・**他部門の回帰**（`/nyanko`・`/tsum` が200）。
- 既存テストは消さない。ブラウザ（Playwright）でライト/ダーク・iPhone幅・コンソールエラー無しと
  価格整合を確認。

## 9. 共通で“タダで”付いてくるもの（実装不要）
- PayPay 決済・保留・再確認・管理者の支払い済み化
- 注文番号での再開（`/api/orders/<id>/claim`）
- 履歴削除（`/api/orders/<id>/forget`・終端状態のみ・管理台帳は保持）
- クーポン（200円以上で発行）
- 実行枠（同時4・待機20）・ドレイン（デプロイ時）
- 管理画面の料金編集（`PRICES`/`PRICE_LABELS` を足せば自動で欄が出る）

---

## 共通エンドポイント（部門非依存）
| メソッド | パス | 用途 |
|---|---|---|
| POST | `/run` | 注文（body に `{<dept>:{...}}`） |
| GET | `/api/orders/mine` | このブラウザの注文（`hidden_by_owner` は除外） |
| POST | `/api/orders/<id>/claim` | 注文番号で再開（hidden も復活） |
| POST | `/api/orders/<id>/pay` | PayPay送金リンクで支払い確認・実行開始 |
| POST | `/api/orders/<id>/forget` | 本人が一覧から削除（done/dead/unpaid のみ） |
| GET | `/status/<job_id>` | 進捗（`total` はモード別） |

## 回帰で踏みやすい3点（ぷにで実際に出た）
1. **画面見積もりとサーバ料金のズレ**（単位変更時）。`unitsForPrice` と `pricing_units` を必ず揃える。
2. **mode なりすまし**。経路はメニューから決める。
3. **他部門への注文の混入**。`loadResume`/home の絞り込みを `o.<dept>` 基準にする。
