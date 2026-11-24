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

### 2026-11-25（追加）自律バグ修正

精査で見つけた実在バグを修正：

| # | バグ | 原因 | 修正 |
|---|---|---|---|
| B-A | **回し放題の料金が画面で過少表示** | P-09 で入力を時間単位に変えたが、画面JS `unitsForPrice` が `ceil(n/60)`（分前提）のまま。3時間が 100円表示・サーバは 300円 | JS を `max(1,n)`（1時間=1件）に。ブラウザで 3h=300円 とサーバ一致を確認 |
| B-B | **mode なりすましで注文が化ける** | `_run_puni_order` が SOLO のとき mode=new に上書きするが、非SOLOで mode=new を強制しない。`mode=new`＋`puni_farm` で資格情報が空にされ、`run_order` が mode=new を見てアカウント作成経路へ → 周回注文がアカウント作成に化ける | app はモードをメニューから決定（非SOLOは必ず email）。`run_order` も経路をメニューで判定（mode を経路判断に使わない）。テスト追加 |
| B-C | **ステージ存在チェックの誤拒否** | `_stage_exists` が最大24hキャッシュのマスタで hard reject。新イベント追加直後の有効IDを誤って弾く。無効IDは battle_start が即失敗するので長い待ちにならない | hard reject をやめ、見つからなくても注意記録のうえ実行。battle 失敗時のエラーに「IDをご確認ください」を追加 |
| B-D | 久垢の初回クリアがログイン用出口のまま | 作成直後の client の出口が login_proxy | 初回クリア前に `_set_client_egress` でゲームAPI用出口へ |

回帰：全ルート 383 passed。ruff clean。ブラウザで価格整合・ライト/ダーク確認。

### 通信・サーバー構成の監査（2026-11-25）
- **tsum との整合**：ツムツムは `TSUM_SOCKS_URL`（Tor/SOCKS）でログインを秘匿し、ゲームAPIは
  既定で直結（`tor_game_api` false・1件で多量通信のため）。ぷにぷにも同じ分離に合わせた：
  `PUNI_SOCKS_URL` でログイン・アカ作成を通し、周回などゲームAPIは既定直結
  （`PUNI_TOR_GAME_API=1` で経由）。`_set_client_egress` でログイン後に出口を切替。
- **タイムアウト**：捨てメール/LEVEL5 登録は `requests timeout=30`。ゲームAPIはエンジンの
  `post_nhn`（timeout=30・retry）。HTTPは gunicorn `--timeout 120`。長時間ジョブは worker 別プロセスで
  走るため gunicorn のHTTPタイムアウトには当たらない。
- **TLS/DNS**：Fly が TLS 終端（`force_https=true`）。送信元の秘匿は SOCKS 設定時のみ。未設定なら
  サーバーの固定IPで直結（tsum と同じ）。意図しない経路変更は無し（プロキシは明示設定のときだけ）。
- **データ保全**：`PUNI_DATA_DIR=/data/puni`（Fly 永続ボリューム）。`fly.toml [env]` に明示追加。
  udkey/ログ/マスタはリポジトリ外。注文 payload（email/パスワード）は `orders.enc`（暗号化）。
  完了時に `_reconcile_orders` が `arguments=None` にしてパスワードを消す（P-01 最小化）。

### 完了条件の確認（2026-11-25）
- [x] 重大な既知バグ：P-02（過剰提供）・P-07（二重入力）・P-16（進捗段階）を修正
- [x] 既存・追加テスト通過：`test_puni.py` 57 passed、全体 226 passed（capture off）。ruff clean
- [x] UI/UX：既存部門と統一・作成フロー簡潔化・注意明示・ちらつき解消。ライト/ダーク/iPhone幅で確認
- [x] 注文・決済整合：部分提供は再実行させず管理者対応へ（outcome_unknown）。完了時パスワード消去
- [x] 顧客情報保護：乱数 secrets 化・ログにパスワード出さない・暗号化保存・完了時消去
- [x] 他部門回帰なし：にゃんこ・ツムツムのコード無変更、両画面200・全テスト通過
- 本番デプロイ・実決済・実機動作は別途承認／確認が必要（この環境では不可）

### 残課題 / 次の作業
- **P-06 専用実行枠**：共通スケジューラ（`_pending_jobs`/`_slots`）を puni 専用に分けるのは、
  にゃんこ・ツムツムへの回帰リスクがあるため本改修では見送り。回し放題は最大6時間に制限して緩和。
  将来案：キュー選択時に puni 長時間ジョブの同時数だけ上限を設ける（共通スケジューラに最小の分岐）。
- **実機確認**：LEVEL5 アカウント・ぷにぷにゲームサーバでの動作は本環境では不可。本番初回は捨て垢で
  `puni_coin_gacha` / `puni_hitodama` を1件通して rc を確認。rc=20 なら APPVER 更新（P-10）。
- **バナー画像**：`static/game-puni.{webp,jpg}`・`static/ogp-puni.jpg` 未配置（home カードの画像は後日）。
- **任意の運用設定**：`PUNI_SOCKS_URL`（送信元秘匿したい場合）・`PUNI_TOR_GAME_API=1`（周回も経由）。
