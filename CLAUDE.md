# CLAUDE.md

このリポジトリの説明・ルール・デプロイ方法はすべて **AGENTS.md** にある（下で読み込んでいる）。作業の前に必ず全部読むこと。

@AGENTS.md

---

## Claude Code で作業するときの補足

### いちばん大事な3つ
- **`main` への push ＝ 本番反映**（利用者がいるお店）。AGENTS.md §5 のテストを全部通してから。
- **リポジトリは公開**。秘密情報・利用者や DM 相手の情報を、コミットにも Actions のログにも出さない。
- **返答・コミットメッセージ・画面の文言は日本語**。依頼主は iPhone で読むので、結論を先に、短く、かみくだいて。

### よく使う手順
- **本番の反映を確かめる**：push したコミットの Actions（`Deploy to VPS`・`CI` など）が成功したか見る → `curl -s -o /dev/null -w "%{http_code}" https://nyanko-daiko-tnshop.duckdns.org/health` が 200。
  - Actions の一覧：`https://api.github.com/repos/tenma2066-tech/CatsDaiko5-Beta/actions/runs?head_sha=<コミット>`（公開リポジトリなので認証なしで見られる）
  - ジョブのログは GitHub の MCP ツール（`get_job_logs`）で読む。
  - 終わるのを待つときは、短い `sleep` を何度もつなげず、条件がそろうまで待つループ（`until ...; do sleep 20; done`）にする。
- **xbot を更新・調査する**：`xbot/deploy/run.txt` の1行目を `install`／`status`／`probe`／`lab` などに書き換え、最後の行の `#番号` を変えて `main` に push（AGENTS.md §4）。
- **`main` に入れる**：`git fetch origin main` → `origin/main` の上に変更を載せて（`git checkout --detach origin/main` → `cherry-pick`）→ `git push origin HEAD:main`。ほかの人のコミットが `main` に入っていることがあるので、作業ブランチをそのまま `main` に push しない。
- **画面を確かめる**：Playwright（Chromium）で `app.py` をローカル起動して開く。スマホ幅（390×844）で見る。決済なしなら `PAYMENTS_ENABLED=0`。

### してはいけないこと
- 依頼主の OK なしに、本番の X で投稿・DM 送信をする／本番で決済を試す。
- 料金（`PRICES`）をコードで変える。
- 古い注文のためのメニュー名・データ（AGENTS.md §3）を消す。
- 秘密情報をチャットで受け取る・コードやログに書く（GitHub Secrets だけ）。
- PR を頼まれていないのに作る。
