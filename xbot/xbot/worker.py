"""常駐プロセス：予約投稿・DM返信・ログイン画面の表示。systemd（xbot-worker.service）で動かす。"""
import json
import os
import random
import shutil
import signal
import subprocess
import time
import traceback

from . import logic, store
from .browser import Browser, ChatLocked, DMBrowser, NotLoggedIn
from . import xlogin

TICK = 20                 # 何秒ごとに見るか
VNC_BIND = os.environ.get("XBOT_VNC_BIND", "127.0.0.1")   # noVNC（websockify）を待ち受けるIP
VNC_WEB_PORT = int(os.environ.get("XBOT_VNC_PORT", "6080"))
NOVNC_DIR = os.environ.get("XBOT_NOVNC_DIR", "/usr/share/novnc")
DISPLAY = ":99"

_stop = False


def _sig(*_):
    global _stop
    _stop = True


# ---------------------------------------------------------------------- 予約投稿
def due_post():
    with store.db() as d:
        return d.execute("SELECT * FROM posts WHERE state='scheduled' AND at<=? ORDER BY at, id LIMIT 1", (time.time(),)).fetchone()


def posted_times():
    with store.db() as d:
        return [r["done_at"] for r in d.execute("SELECT done_at FROM posts WHERE state='posted' AND done_at>=?", (time.time() - 2 * 86400,))]


def _finish_repeat(row, state, shot, error, text):
    """定期投稿：今回の結果を記録として残し、元の予約は次の時刻へ進める。"""
    rep = json.loads(row["repeat"])
    nxt = logic.next_time(rep, max(time.time(), row["at"]))
    with store.db() as d:
        d.execute("INSERT INTO posts(text, images, at, state, created, done_at, error, shot, parent) VALUES(?,?,?,?,?,?,?,?,?)",
                  (text, row["images"], row["at"], state, time.time(), time.time(), error, shot, row["id"]))
        if nxt:
            d.execute("UPDATE posts SET state='scheduled', at=?, tries=0, error=NULL, done_at=?, shot=? WHERE id=?", (nxt, time.time(), shot, row["id"]))
        else:
            d.execute("UPDATE posts SET state='failed', error='次の時刻を計算できませんでした' WHERE id=?", (row["id"],))
    return nxt


def run_post(s, row):
    rep = row["repeat"] if "repeat" in row.keys() else None
    if not logic.can_post(posted_times(), time.time(), int(s["max_posts_day"])):
        if rep:   # 定期投稿は、上限の日は1回休んで次の時刻へ
            nxt = _finish_repeat(row, "failed", None, "1日の投稿数の上限のため、今回は休みました", row["text"])
            store.log("warn", "post", f"#{row['id']} 上限のため今回は休み（次は {logic.fmt_jst(nxt)}）")
        else:
            store.update_status(note=f"今日の投稿は上限（{s['max_posts_day']}件）に達したため、明日に回します")
        return
    images = [os.path.join(store.UPLOAD_DIR, f) for f in json.loads(row["images"] or "[]")]
    missing = [p for p in images if not os.path.exists(p)]
    text = logic.render_text(row["text"], time.time())
    with store.db() as d:
        d.execute("UPDATE posts SET state='posting', tries=tries+1 WHERE id=?", (row["id"],))
    dry = bool(s["dry_run"])
    try:
        if missing:
            raise RuntimeError("画像ファイルが見つかりません")
        with Browser() as b:
            shot = b.post(text, images, dry)
        state = "dry" if dry else "posted"
        if rep:
            nxt = _finish_repeat(row, state, shot, None, text)
            store.log("info", "post", f"#{row['id']}（定期）を{'下書きモードで確認しました（投稿していません）' if dry else '投稿しました'}。次は {logic.fmt_jst(nxt)}")
            return
        with store.db() as d:
            d.execute("UPDATE posts SET state=?, done_at=?, shot=?, error=NULL WHERE id=?", (state, time.time(), shot, row["id"]))
        store.log("info", "post", f"#{row['id']} を{'下書きモードで確認しました（投稿していません）' if dry else '投稿しました'}")
    except NotLoggedIn as e:
        with store.db() as d:
            d.execute("UPDATE posts SET state='scheduled' WHERE id=?", (row["id"],))
        store.update_status(logged_in=False)
        store.log("error", "login", str(e))
    except Exception as e:  # noqa: BLE001
        retry = row["tries"] + 1 < 3
        if not retry and rep:
            nxt = _finish_repeat(row, "failed", None, str(e)[:500], text)
            store.log("error", "post", f"#{row['id']}（定期）3回失敗したので今回は休み（次は {logic.fmt_jst(nxt)}）: {e}")
            return
        with store.db() as d:
            d.execute("UPDATE posts SET state=?, error=?, at=? WHERE id=?",
                      ("scheduled" if retry else "failed", str(e)[:500], time.time() + 600 if retry else row["at"], row["id"]))
        store.log("error", "post", f"#{row['id']} 失敗{'（10分後にもう一度）' if retry else '（3回失敗したので止めました）'}: {e}")


# ---------------------------------------------------------------------- DM
def rules():
    with store.db() as d:
        return [dict(r) for r in d.execute("SELECT * FROM rules WHERE enabled=1")]


def reply_times():
    with store.db() as d:
        return [r["at"] for r in d.execute("SELECT at FROM replied WHERE at>=? AND dry=0", (time.time() - 3600,))]


def last_reply_at(conv):
    with store.db() as d:
        r = d.execute("SELECT MAX(at) AS t FROM replied WHERE conv=? AND dry=0", (conv,)).fetchone()
    return r["t"] if r else None


def already(conv, h):
    """このメッセージは処理済みか。
    ・本番で返信したもの → 処理済み
    ・下書きモードで下書きしただけ → 処理済みにしない（本番にしたら返信する）
    ・当てはまるルールがなかった → ルールを変えるまでは処理済み（変えたら、もう一度見る）"""
    changed = float(store.get("rules_changed") or 0)
    with store.db() as d:
        return d.execute("SELECT 1 FROM replied WHERE conv=? AND msg_hash=? AND (dry=0 OR (rule_id IS NULL AND at>?))",
                         (conv, h, changed)).fetchone() is not None


def _handle(b, s, rs, key, got, dry, counts, who=None):
    """1つの会話：最後が相手のメッセージで、ルールに合えば返信する。"""
    if not got:
        return
    message, mine, first = got
    if mine:
        return
    h = logic.msg_hash(key, message)
    if already(key, h):
        return
    if not logic.can_reply(reply_times(), time.time(), int(s["max_replies_hour"])):
        counts["limited"] = True
        return
    if logic.in_cooldown(last_reply_at(key), time.time(), float(s["reply_cooldown_hours"])):
        return
    rule = logic.pick_rule(rs, message, first)
    if rule and rule["kind"] == "handoff":
        # 要対応：自動では答えない内容（返金・トラブルなど）。自分に知らせる。返信文があればそれだけ送る
        if who is None:
            who = b.dm_peer()
        with store.db() as d:
            new = d.execute("INSERT OR IGNORE INTO handoffs(conv, msg_hash, who, rule_id, snippet, at) VALUES(?,?,?,?,?,?)",
                            (key, h, who or None, rule["id"], (message or "")[:200], time.time())).rowcount
        if new:
            store.log("warn", "handoff", f"要対応：{who + ' から' if who else ''}ルール「{rule['name']}」に当てはまるDMが来ました（管理サイトの「DM返信」で確認）")
        counts["handoff"] = counts.get("handoff", 0) + (1 if new else 0)
        if not (rule["reply"] or "").strip():
            with store.db() as d:   # 返信はしないが、同じメッセージを何度も数えない
                d.execute("INSERT INTO replied(conv, msg_hash, rule_id, at, dry, who) VALUES(?,?,?,?,0,?) "
                          "ON CONFLICT(conv, msg_hash) DO NOTHING", (key, h, rule["id"], time.time(), who or None))
            return
    if not rule:
        with store.db() as d:   # 当てはまるルールがない → 次からは見ない
            d.execute("INSERT INTO replied(conv, msg_hash, rule_id, at, dry) VALUES(?,?,?,?,1) "
                      "ON CONFLICT(conv, msg_hash) DO UPDATE SET rule_id=NULL, at=excluded.at, dry=1", (key, h, None, time.time()))
        return
    if who is None:
        who = b.dm_peer()
    b.dm_reply(logic.render_reply(rule["reply"], who), dry)
    with store.db() as d:
        d.execute("INSERT INTO replied(conv, msg_hash, rule_id, at, dry, who) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(conv, msg_hash) DO UPDATE SET rule_id=excluded.rule_id, at=excluded.at, dry=excluded.dry, who=excluded.who",
                  (key, h, rule["id"], time.time(), 1 if dry else 0, who or None))
    counts["replied"] += 1
    store.log("info", "dm", f"{who + ' に' if who else ''}ルール「{rule['name']}」で{'返信を下書きしました（送っていません）' if dry else '返信しました'}")
    time.sleep(random.uniform(20, 60))   # 続けて返さない


DM_RUN_MAX_SEC = 10 * 60     # 1回のDM確認の長さの上限（これを超えたら次の回へ）
OPEN_FAIL_STOP = 2           # 会話を開けないのが続いたら、その回は打ち切る（タイムアウトを積み重ねない）


class _Stop(Exception):
    """DM確認を途中で切り上げる（予約投稿の時刻が来た・時間切れ）。"""


def _checkpoint(counts, started, yield_to_post):
    if yield_to_post and yield_to_post():
        counts["yielded"] = True
        raise _Stop()
    if time.time() - started > DM_RUN_MAX_SEC:
        counts["timeout"] = True
        raise _Stop()


def run_dms(s, manual=False, yield_to_post=None):
    """DMを確認する。yield_to_post() が真を返したら、会話の区切りで切り上げて予約投稿を先にする。
    戻り値は counts（yielded=切り上げた、timeout=時間切れ、open_fail=開けずに打ち切った）。"""
    started = time.time()
    try:
        return _run_dms(s, manual, yield_to_post, started)
    finally:
        store.update_status(last_dm_secs=int(time.time() - started))


def _run_dms(s, manual, yield_to_post, started):
    rs = rules()
    dry = bool(s["dry_run"])
    counts = {"replied": 0, "accepted": 0, "requests": 0, "chats": 0, "limited": False, "handoff": 0}
    if not rs and not s.get("dm_accept_requests"):
        store.update_status(last_dm_check=time.time(), dm_checked=0, dm_replied=0)
        if manual:
            store.log("warn", "dm", "DMの確認：返信ルールが1つもありません（「DM返信」タブで追加してください）")
        return counts
    try:
        _dm_body(s, rs, dry, counts, started, yield_to_post, manual=manual)
    except _Stop:
        if counts.get("yielded"):
            store.log("info", "dm", "予約投稿の時刻になったので、DMの確認をいったん切り上げました（投稿のあとで続けます）")
        else:
            store.log("warn", "dm", f"DMの確認が{DM_RUN_MAX_SEC // 60}分を超えたので、今回はここまでにしました")
    store.update_status(last_dm_check=time.time(), dm_checked=counts["chats"], dm_replied=counts["replied"],
                        dm_requests=counts["requests"], dm_accepted=counts["accepted"], dm_handoff=counts["handoff"])
    if manual or counts["replied"] or counts["accepted"]:
        req = (f"リクエスト {counts['requests']}件" + (f"（許可 {counts['accepted']}件）" if not dry else "（下書きモードなので許可していません）")) if s.get("dm_accept_requests", True) else "リクエストは自動許可オフ"
        store.log("info", "dm", f"DMを確認しました：会話 {counts['chats']}件、{req}、{'下書き' if dry else '返信'} {counts['replied']}件"
                  + (f"、要対応 {counts['handoff']}件" if counts["handoff"] else ""))
    return counts


OTHER_EVERY_SEC = 60 * 60   # 「その他」のリクエストを見る間隔（自動のとき）。開くと Cloudflare に警戒されるので、たまにだけ


def _dm_body(s, rs, dry, counts, started, yield_to_post, manual=False):
    """ふつうの会話 → リクエスト（メイン）→ リクエスト（その他）の順に、それぞれ別のブラウザで確認する。

    実験（xbot/lab、2026-11-22。各2回くり返して同じ結果）で分かったこと：
      ・リクエストの「その他」を開いたブラウザは、起動し直すまで URL で開いた DM 画面が描かれない
        （会話の一覧・リクエスト画面とも。再読み込み・新しいタブでも直らない。画面の中のリンクで戻っても、次に URL を開くとまた壊れる）
      ・メインのリクエスト画面だけなら、そのあと会話の一覧も普通に出る
      ・原因は X の前にある Cloudflare：「その他」を押すと cf_clearance・__cf_bm が書き換わり、
        そのブラウザからは X のどのページ（/home も）も 403 になる（ボットと判定された）
    なので「その他」は、自動では1時間に1回だけ・ブラウザの最後の仕事にして、1回に1件だけ許可する。
    Cloudflare の判定をすり抜ける細工（Cookie を戻すなど）はしない。"""
    if rs:
        with DMBrowser() as b:
            _dm_chats(b, s, rs, dry, counts, started, yield_to_post)
    if not s.get("dm_accept_requests", True):
        return
    _checkpoint(counts, started, yield_to_post)
    with DMBrowser() as b:
        _dm_requests_main(b, s, rs, dry, counts, started, yield_to_post)
    last = float((s.get("status") or {}).get("dm_other_at") or 0)
    if manual or time.time() - last >= OTHER_EVERY_SEC:
        store.update_status(dm_other_at=time.time())
        _dm_requests_other(s, rs, dry, counts, started, yield_to_post)


def _accept_one(b, s, rs, key, dry, counts, scope):
    got = b.dm_open_request(key)
    b.dm_accept()
    who = b.dm_peer()
    counts["accepted"] += 1
    store.log("info", "dm", f"{scope}を許可しました{'：' + who if who else ''}")
    if rs:
        _handle(b, s, rs, key, got, dry, counts, who=who)


def _dm_requests_main(b, s, rs, dry, counts, started, yield_to_post):
    """メインのメッセージリクエスト：許可して、ルールに合えば返信。失敗しても止めない。"""
    scope = "リクエスト"
    try:
        b.dm_open_inbox(requests=True)
        keys = b.dm_request_keys()
        counts["requests"] += len(keys)
        seen = set()
        for _ in range(0 if dry else min(5, len(keys))):   # 下書きモードでは許可しない（件数だけ知らせる）
            _checkpoint(counts, started, yield_to_post)
            b.dm_open_inbox(requests=True)
            cur = [k for k in b.dm_request_keys() if k not in seen]   # 許可すると一覧が変わるので、毎回読み直す
            if not cur:
                break
            seen.add(cur[0])
            _accept_one(b, s, rs, cur[0], dry, counts, scope)
            time.sleep(random.uniform(5, 15))
    except (ChatLocked, _Stop):
        raise
    except Exception as e:  # noqa: BLE001
        b.shot("dm-request-fail")
        store.log("warn", "dm", f"{scope}の処理でつまずきました（{type(e).__name__}）。続きの確認は進めます")


def _dm_requests_other(s, rs, dry, counts, started, yield_to_post):
    """隠れている「その他」のリクエスト：新しいブラウザで開き、1件だけ許可する（開いたあとは、そのブラウザでは他のことをしない）。"""
    scope = "リクエスト（その他）"
    _checkpoint(counts, started, yield_to_post)
    try:
        with DMBrowser() as b:
            b.dm_open_inbox(requests=True, other=True)
            keys = b.dm_request_keys()
            counts["requests"] += len(keys)
            if dry or not keys:
                return
            _accept_one(b, s, rs, keys[0], dry, counts, scope)
    except (ChatLocked, _Stop):
        raise
    except Exception as e:  # noqa: BLE001
        store.log("warn", "dm", f"{scope}の処理でつまずきました（{type(e).__name__}）")


def _dm_chats(b, s, rs, dry, counts, started, yield_to_post):
    """ふつうの会話：一覧を読み、1件ずつ開いて、最後が相手のメッセージならルールで返信。"""
    b.dm_open_inbox()
    keys = b.dm_chat_keys(int(s["dm_scan_limit"]))
    counts["chats"] = len(keys)
    fails = 0
    for key in keys:
        if counts["limited"]:
            store.update_status(note=f"1時間の返信数が上限（{s['max_replies_hour']}件）に達したため、少し待ちます")
            break
        _checkpoint(counts, started, yield_to_post)
        b.dm_open_inbox()
        try:
            got = b.dm_open_chat(key)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            store.log("warn", "dm", f"会話を開けませんでした（{type(e).__name__}）")
            if fails >= OPEN_FAIL_STOP:
                counts["open_fail"] = True
                store.log("warn", "dm", f"会話を開けないのが{fails}回続いたので、今回の確認はここまでにしました")
                break
            continue
        _handle(b, s, rs, key, got, dry, counts)


def _post_due_now():
    return due_post() is not None and not store.get_all()["paused"]


def _auto_dm(s):
    """自動のDM確認を1回行い、次に確認する時刻を返す。
    ・予約投稿の時刻が来たら会話の区切りで切り上げ、投稿のあとすぐ続ける
    ・失敗が続いたら間隔を倍々にのばす（最大3時間）。DMがだめでも予約投稿は止めない"""
    st = s.get("status") or {}
    streak = int(st.get("dm_fail_streak") or 0)
    failed = False
    yielded = False
    try:
        counts = run_dms(s, yield_to_post=_post_due_now) or {}
        yielded = bool(counts.get("yielded"))
        failed = bool(counts.get("open_fail"))
    except NotLoggedIn as e:
        failed = True
        store.update_status(logged_in=False)
        store.log("error", "login", str(e))
    except ChatLocked as e:
        failed = True
        store.update_status(chat_unlocked=False)
        store.log("error", "dm", str(e))
    except Exception as e:  # noqa: BLE001
        failed = True
        store.log("error", "dm", f"DMの確認に失敗しました: {e}")
    streak = streak + 1 if failed else 0
    store.update_status(dm_fail_streak=streak)
    if yielded:
        return time.time() + 5
    delay = logic.dm_next_delay(s["dm_interval_min"], streak)
    if failed and streak >= 2:
        store.log("warn", "dm", f"DMの確認がうまくいかない状態が{streak}回続いています。次は {int(delay // 60)}分後に確認します（予約投稿はそのまま動きます）")
    return time.time() + delay + random.uniform(0, 60)


# ---------------------------------------------------------------------- ログイン画面（Xvfb + x11vnc + noVNC）
class LoginScreen:
    def __init__(self):
        self.procs = []
        self.browser = None

    def running(self):
        return bool(self.procs)

    def start(self, password):
        for tool in ("Xvfb", "x11vnc", "websockify"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} が入っていません")
        self.procs.append(subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "430x900x24", "-nolisten", "tcp"]))
        time.sleep(1.5)
        pwfile = os.path.join(store.DATA_DIR, ".vncpass")
        subprocess.run(["x11vnc", "-storepasswd", password, pwfile], check=True, capture_output=True)
        os.chmod(pwfile, 0o600)
        self.procs.append(subprocess.Popen(["x11vnc", "-display", DISPLAY, "-rfbport", "5901", "-localhost", "-rfbauth", pwfile,
                                            "-forever", "-shared", "-quiet", "-noxdamage"]))
        self.procs.append(subprocess.Popen(["websockify", "--web", NOVNC_DIR, f"{VNC_BIND}:{VNC_WEB_PORT}", "127.0.0.1:5901"]))
        time.sleep(1)
        self.browser = Browser(headless=False, display=DISPLAY).__enter__()
        self.browser.page.goto("https://x.com/login")
        store.log("info", "login", "ログイン画面を開きました（15分で自動的に閉じます）")

    def stop(self):
        if self.browser:
            try:
                self.browser.__exit__(None, None, None)
            except Exception:
                pass
            self.browser = None
        for p in reversed(self.procs):
            try:
                p.terminate()
                p.wait(5)
            except Exception:
                p.kill()
        self.procs = []
        try:
            os.remove(os.path.join(store.DATA_DIR, ".vncpass"))
        except OSError:
            pass
        store.put("vnc_password", "")
        store.log("info", "login", "ログイン画面を閉じました")


def check_login():
    try:
        with Browser() as b:
            ok = b.logged_in()
            shot = b.shot("login-check")
            names = sorted({c["name"] for c in b.ctx.cookies("https://x.com")})
            url = b.page.url.split("?")[0]
            exit_ = b.exit_info()
        store.log("info", "login", f"ログイン確認：auth_token={'あり' if 'auth_token' in names else 'なし'} 項目={len(names)}件 画面={url} {exit_}")
        store.update_status(logged_in=ok, login_checked=time.time(), login_shot=shot)
        store.log("info" if ok else "warn", "login", "Xにログインしています" if ok else "Xにログインしていません")
    except Exception as e:  # noqa: BLE001
        store.log("error", "login", f"ログイン状態を確認できませんでした: {e}")


def snapshot():
    try:
        with Browser() as b:
            shots = b.snapshot()
        store.update_status(snapshots=shots, snapshot_at=time.time())
        store.log("info", "check", "画面の記録を撮りました（何も送っていません）")
    except Exception as e:  # noqa: BLE001
        store.log("error", "check", f"画面の記録を撮れませんでした: {e}")


def main():
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    store.log("info", "start", "常駐プロセスを起動しました")
    with store.db() as d:   # 投稿の途中で止まったもの：二重投稿を避けるため、自動ではやり直さない
        n = d.execute("UPDATE posts SET state='failed', error='投稿の途中で止まりました。X で投稿されたか確認してから「もう一度予約」してください' "
                      "WHERE state='posting'").rowcount
    if n:
        store.log("warn", "post", f"投稿の途中で止まっていた予約が {n}件 ありました（予約の一覧を確認してください）")
    xlogin.serve()   # 管理サイトから入力してログインするための窓口（127.0.0.1 のみ）
    screen = LoginScreen()
    next_dm = 0
    while not _stop:
        try:
            s = store.get_all()
            store.update_status(heartbeat=time.time())
            if xlogin.MANAGER.active():      # ログイン中はブラウザ（プロフィール）を使わない
                time.sleep(2)
                continue
            req = s.get("request") or ""
            if req:
                store.put("request", "")
            # --- ログイン画面
            if screen.running():
                if time.time() > float(s["login_until"]) or req == "stop_login":
                    screen.stop()
                    store.put("login_until", 0)
                    check_login()
                time.sleep(2)
                continue
            if time.time() < float(s["login_until"]) and s.get("vnc_password"):
                try:
                    screen.start(s["vnc_password"])
                except Exception as e:  # noqa: BLE001
                    store.log("error", "login", f"ログイン画面を開けませんでした: {e}")
                    screen.stop()
                    store.put("login_until", 0)
                continue
            if req == "check_login":
                check_login()
            elif req == "snapshot":
                snapshot()
            elif req == "dm_probe":            # DM画面の診断（読むだけ・何も送らない・件数と部品名だけ記録）
                try:
                    from .browser import CHAT, DMBrowser
                    with DMBrowser() as b:
                        for label, url in (("chat-direct", CHAT), ("requests", CHAT + "/requests"), ("chat-after-requests", CHAT)):
                            b.goto(url)
                            for t in (3, 8, 15, 25):
                                time.sleep(t - (0 if t == 3 else {8: 3, 15: 8, 25: 15}[t]))
                                n_conv = b.page.locator('[data-testid^="dm-conversation-item-"]').count()
                                n_req = b.page.locator('[data-testid^="dm-message-request-item-"]').count()
                                other = b.page.locator('[data-testid="dm-message-requests-other-button"]').count()
                                row = b.page.locator('[data-testid="dm-message-requests-inbox-row"]').count()
                                pin = b.page.locator('[data-testid="pin-code-input-container"]').count()
                                print(f"[probe] {label} t={t}s url={b.page.url.split('?')[0].rsplit('/', 1)[-1][:12]} conv={n_conv} req={n_req} other_btn={other} req_row={row} pin={pin}", flush=True)
                        b.shot("probe-chat")
                    store.log("info", "check", "DM画面を調べました（何も送っていません）")
                except Exception as e:  # noqa: BLE001
                    store.log("error", "check", f"DM画面を調べられませんでした: {type(e).__name__}")
            elif req == "check_dm":            # 管理サイトの「今すぐDMを確認」
                try:
                    run_dms(s, manual=True)
                except NotLoggedIn as e:
                    store.update_status(logged_in=False)
                    store.log("error", "login", str(e))
                except ChatLocked as e:
                    store.update_status(chat_unlocked=False)
                    store.log("error", "dm", str(e))
                except Exception as e:  # noqa: BLE001
                    store.log("error", "dm", f"DMの確認に失敗しました: {e}")
                next_dm = time.time() + max(2, int(s["dm_interval_min"])) * 60
            if s["paused"]:
                time.sleep(TICK)
                continue
            row = due_post()
            if row:
                run_post(s, row)
            if s["dm_enabled"] and time.time() >= next_dm:
                next_dm = _auto_dm(s)
        except Exception:  # noqa: BLE001
            store.log("error", "loop", traceback.format_exc()[-800:])
        for _ in range(TICK):
            if _stop:
                break
            time.sleep(1)
    screen.stop()
    store.log("info", "stop", "常駐プロセスを止めました")


if __name__ == "__main__":
    main()
