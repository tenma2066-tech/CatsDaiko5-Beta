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
    if not rule:
        with store.db() as d:   # 当てはまるルールがない → 次からは見ない
            d.execute("INSERT INTO replied(conv, msg_hash, rule_id, at, dry) VALUES(?,?,?,?,1) "
                      "ON CONFLICT(conv, msg_hash) DO UPDATE SET rule_id=NULL, at=excluded.at, dry=1", (key, h, None, time.time()))
        return
    if who is None:
        who = b.dm_peer()
    b.dm_reply(rule["reply"], dry)
    with store.db() as d:
        d.execute("INSERT INTO replied(conv, msg_hash, rule_id, at, dry, who) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(conv, msg_hash) DO UPDATE SET rule_id=excluded.rule_id, at=excluded.at, dry=excluded.dry, who=excluded.who",
                  (key, h, rule["id"], time.time(), 1 if dry else 0, who or None))
    counts["replied"] += 1
    store.log("info", "dm", f"{who + ' に' if who else ''}ルール「{rule['name']}」で{'返信を下書きしました（送っていません）' if dry else '返信しました'}")
    time.sleep(random.uniform(20, 60))   # 続けて返さない


def run_dms(s, manual=False):
    rs = rules()
    dry = bool(s["dry_run"])
    counts = {"replied": 0, "accepted": 0, "requests": 0, "chats": 0, "limited": False}
    if not rs and not s.get("dm_accept_requests"):
        store.update_status(last_dm_check=time.time(), dm_checked=0, dm_replied=0)
        if manual:
            store.log("warn", "dm", "DMの確認：返信ルールが1つもありません（「DM返信」タブで追加してください）")
        return
    # 1) ふつうの会話：一覧だけ先に読み、会話ごとに新しいブラウザで開く
    #    （X のチャットは、同じブラウザで別の会話・リクエスト画面へ移ると表示されなくなるため）
    keys = []
    if rs:
        with DMBrowser() as b:
            b.dm_open_inbox()
            keys = b.dm_chat_keys(int(s["dm_scan_limit"]))
        counts["chats"] = len(keys)
        for key in keys:
            if counts["limited"]:
                store.update_status(note=f"1時間の返信数が上限（{s['max_replies_hour']}件）に達したため、少し待ちます")
                break
            try:
                with DMBrowser(sync=False) as b:
                    got = b.dm_open_chat(key)
                    _handle(b, s, rs, key, got, dry, counts)
            except ChatLocked:
                raise
            except Exception as e:  # noqa: BLE001
                store.log("warn", "dm", f"会話を開けませんでした（{type(e).__name__}）")
    # 2) メッセージリクエスト（新しいブラウザで）
    with DMBrowser(sync=False) as b:
        # 2) メッセージリクエスト（メインと、隠れている「その他」の両方）：許可して、ルールに合えば返信
        #    失敗しても、ふつうの会話の確認は続ける
        if s.get("dm_accept_requests", True):
            for scope, other in (("リクエスト", False), ("リクエスト（その他）", True)):
                try:
                    b.dm_open_inbox(requests=True, other=other)
                    keys = b.dm_request_keys()
                    counts["requests"] += len(keys)
                    seen = set()
                    for _ in range(0 if dry else min(5, len(keys))):   # 下書きモードでは許可しない（件数だけ知らせる）
                        b.dm_open_inbox(requests=True, other=other)
                        cur = [k for k in b.dm_request_keys() if k not in seen]   # 許可すると一覧が変わるので、毎回読み直す
                        if not cur:
                            break
                        key = cur[0]
                        seen.add(key)
                        got = b.dm_open_request(key)
                        b.dm_accept()
                        who = b.dm_peer()
                        counts["accepted"] += 1
                        store.log("info", "dm", f"{scope}を許可しました{'：' + who if who else ''}")
                        if rs:
                            _handle(b, s, rs, key, got, dry, counts, who=who)
                        time.sleep(random.uniform(5, 15))
                except ChatLocked:
                    raise
                except Exception as e:  # noqa: BLE001
                    b.shot("dm-request-fail")
                    store.log("warn", "dm", f"{scope}の処理でつまずきました（{type(e).__name__}）。続きの確認は進めます")
    store.update_status(last_dm_check=time.time(), dm_checked=counts["chats"], dm_replied=counts["replied"],
                        dm_requests=counts["requests"], dm_accepted=counts["accepted"])
    if manual or counts["replied"] or counts["accepted"]:
        req = (f"リクエスト {counts['requests']}件" + (f"（許可 {counts['accepted']}件）" if not dry else "（下書きモードなので許可していません）")) if s.get("dm_accept_requests", True) else "リクエストは自動許可オフ"
        store.log("info", "dm", f"DMを確認しました：会話 {counts['chats']}件、{req}、{'下書き' if dry else '返信'} {counts['replied']}件")


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
                        b.dm_open_inbox()
                        print(f"[probe] 1) chats first: conv={len(b.dm_chat_keys(50))}", flush=True)
                        b.dm_open_inbox(requests=True)
                        print(f"[probe] 2) requests: req={len(b.dm_request_keys())}", flush=True)
                        b.dm_open_inbox(requests=True, other=True)
                        print(f"[probe] 3) other: req={len(b.dm_request_keys())} no_items={getattr(b, 'no_items', None)}", flush=True)
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
                next_dm = time.time() + max(2, int(s["dm_interval_min"])) * 60 + random.uniform(0, 60)
                try:
                    run_dms(s)
                except NotLoggedIn as e:
                    store.update_status(logged_in=False)
                    store.log("error", "login", str(e))
                except ChatLocked as e:
                    store.update_status(chat_unlocked=False)
                    store.log("error", "dm", str(e))
                except Exception as e:  # noqa: BLE001
                    store.log("error", "dm", f"DMの確認に失敗しました: {e}")
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
