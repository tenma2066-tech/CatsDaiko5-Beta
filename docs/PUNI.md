# ぷにぷに代行：開発記録

TN SHOP の3つ目の大メニュー（妖怪ウォッチ ぷにぷに）の実装・改修記録。
にゃんこ＝`engine.py`、ツムツム＝`tsum_service.py` と同じ位置に `puni_service.py` を置き、
ゲーム実処理は `puni_engine/`（配布v2・未改変）が行う。注文・支払い・実行枠・台帳は
にゃんこ／ツムツムと共通基盤を使う。

このファイルは、コンテキストが切れても作業を正確に再開できるように随時更新する。

---

## 全体の作り（再開用サマリ）

| 層 | ファイル | 役割 |
|---|---|---|
| 画面 | `templates/puni.html`（`/puni`） | メニュー選択・入力・注文・決済・状況 |
| サービス | `puni_service.py` | メニュー表・入力検査・料金単位・`run_order`（実処理） |
| エンジン | `puni_engine/`（未改変） | `ywp_auto.py`（ゲームAPI）・`ywp_master.py`・`proxy_pool.py` |
| 料金 | `pricing.py` | `PRICES["puni_*"]`・`compute_puni_amount` |
| 注文/実行 | `app.py` | `/puni`・`_run_puni_order`・`_run_puni_job`・worker分岐 |
| 実行時データ | `PUNI_DATA_DIR`（既定 `/data/puni`、無ければ `instance/puni`） | udkey/ログ/マスタ（リポジトリには書かない） |

通信：`PUNI_SOCKS_URL`（SOCKS/HTTP、任意。tsum の `TSUM_SOCKS_URL` と同じ役割）。
ログイン・アカウント作成はこの出口を通し、周回などゲームAPIの多量通信は既定で直結
（`PUNI_TOR_GAME_API=1` で周回もプロキシ経由）。tsum と同じ「ログインは秘匿／ゲームAPIは直結」の分離。

---

## 2026-11-25 改修（本ブランチ：claude/zen-pasteur-6nra8z）

### 現状分析で実コードから確認した問題（P-01〜P-17）

| 監査 | 実コード上の事実 | 対応方針 |
|---|---|---|
| P-01 顧客パスワード | email ログインのパスワードは worker 引数（payload）にのみ存在し、ディスクには書かない。ログは `_mask_email` でメールだけ伏せる（パスワードは出さない）。注文復元 payload は `orders.enc`（暗号化）に入る | 保存最小化を維持。注文復元時のパスワード保持を見直し（下記） |
| P-02 部分成功と決済 | `_run_puni_job` のエラー分岐で `outcome_unknown` が account 以外では False。周回3/10で致命→`else`分岐→`paid`+`retry` に戻り**再実行で過剰提供** | done>0 のとき `outcome_unknown=True` にして自動再実行を止める（済） |
| P-03 BAN リスク | 自動周回・自動アカウント作成は規約上アカウント停止の可能性 | UI に実務的な1行注記＋docs 明記（済） |
| P-04 使い捨てメール | `_create_level5_id` は GuerrillaMail の受信できないアドレスで ID 作成。顧客はパス再発行不可 | UI・結果表示・docs で明示（済） |
| P-05 注文番号復元 | `/api/orders/<id>/claim` は推測不能な `secrets.token_urlsafe(16)`。失敗回数制限あり（共通） | 現状維持（共通基盤） |
| P-06 長時間ジョブ | `MAX_CONCURRENT_JOBS=4` 共有。回し放題（時間制）が枠を長時間占有 | 上限を 6 時間に制限し時間単位課金と一致（済）。専用枠は共通スケジューラ改変リスクのため見送り（下記 残課題） |
| P-07 ステージ二重指定 | `puni_stage_clear` が `value:(1,99999999)` かつ `STAGE_MENUS`。数量欄とステージ欄が両方ステージIDを要求 | `value` を外しステージ欄のみに（済） |
| P-08 ステージ存在確認 | 入力IDの実在を未確認。誤IDで長い再試行ループ | ログイン後にマスタ照合し、未知IDは早期に明確なエラー（済） |
| P-09 時間/料金不整合 | 入力 30 分単位・課金 60 分単位 | 入力を時間単位（1〜6時間）に統一し課金と一致（済） |
| P-10 APPVER 固定 | `ywp_auto.APPVER='4.175.0'` 固定。ゲーム更新で rc=20 | エンジン未改変のため rc=20 を検出し「要バージョン更新」を明確化＋docs（済） |
| P-11 マスタ鮮度 | `master_table` は 24h キャッシュ（data 配下） | 現状の鮮度で可。rc=20/マスタ不整合時のエラーを明確化 |
| P-12 初期化競合 | `_prepare_engine` の `_core_lock` は threading（プロセス内のみ）。worker 複数プロセスでコピー競合 | fcntl ファイルロック＋原子的コピー（済） |
| P-13 mtime 依存 | `_prepare_engine` が mtime 比較でコピー判定。/data 永続時にずれる余地 | mtime かサイズ差でコピー（済） |
| P-14 乱数品質 | `_gen_password` が `random.choice`（非暗号） | `secrets.choice`＋文字種保証（済） |
| P-15 テスト不足 | `test_puni.py` は検査・料金・dispatch 中心 | 状態遷移・並列・権限・再起動・異常系・UI・回帰を拡充（済） |
| P-16 進捗段階 | `status` API が全モード `total=TOTAL_STEPS`(4)。puni は step を 10 まで使う | `total` をモード別に（済） |
| P-17 メール検証 | `"@" in email` のみ | 実用的な検証（local@domain.tld・制御文字・長さ）（済） |

### 変更ファイル（随時更新）
- `puni_service.py`：P-07（stage_clear の value 削除）・P-09（unlimited 時間単位）・P-12/P-13（_prepare_engine にfcntlロック＋原子コピー＋サイズ/mtime判定）・P-14（_gen_password を secrets 化）・P-17（valid_email 追加）・P-10（rc=20 検出）・P-08（_stage_exists でマスタ照合）・通信分離（_login_proxy/_game_api_proxy/_set_client_egress）・P-02（_ActionPartial と out["delivered"]）
- `app.py`：P-16（status の total をモード別）・P-02（_run_puni_job の outcome_unknown を delivered 基準に）
- `pricing.py`：unlimited を時間単位で課金（P-09）
- `test_puni.py`：email/password/unlimited/stage_clear のテスト更新・追加

### テスト結果（2026-11-25 時点）
- `test_puni.py` 30 passed
- 回帰：`test_puni test_seo test_order_flow test_price_parity test_tsum test_ui_results` → 155 passed / 14 skipped（capture off）。にゃんこ・ツムツム無傷

### 残課題 / 次の作業
- P-06 専用実行枠：共通スケジューラ（`_pending_jobs`/`_slots`）を puni 専用に分けるのは、にゃんこ・ツムツムへの回帰リスクがあるため本改修では見送り。回し放題の上限時間制限で緩和。将来、キュー選択時に puni 長時間ジョブの同時数を制限する案。
- 実機（LEVEL5 アカウント・ぷにぷにゲームサーバ）での動作確認は本環境では不可。本番初回は捨て垢で coin_gacha / hitodama を1件通して rc を確認。
- バナー画像 `static/game-puni.{webp,jpg}`・`static/ogp-puni.jpg` 未配置。
