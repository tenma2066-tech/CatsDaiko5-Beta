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
from .browser import Browser, NotLoggedIn
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
    with store.db() as d:
        return d.execute("SELECT 1 FROM replied WHERE conv=? AND msg_hash=?", (conv, h)).fetchone() is not None


def run_dms(s, manual=False):
    rs = rules()
    if not rs:
        store.update_status(last_dm_check=time.time(), dm_checked=0, dm_replied=0)
        if manual:
            store.log("warn", "dm", "DMの確認：返信ルールが1つもありません（「DM返信」タブで追加してください）")
        return
    dry = bool(s["dry_run"])
    done = 0
    with Browser() as b:
        convs = b.list_conversations(int(s["dm_scan_limit"]))
        for c in convs:
            if logic.is_own_preview(c["preview"]):
                continue
            h = logic.msg_hash(c["conv"], c["preview"])
            if already(c["conv"], h):
                continue
            if not logic.can_reply(reply_times(), time.time(), int(s["max_replies_hour"])):
                store.update_status(note=f"1時間の返信数が上限（{s['max_replies_hour']}件）に達したため、少し待ちます")
                break
            if logic.in_cooldown(last_reply_at(c["conv"]), time.time(), float(s["reply_cooldown_hours"])):
                continue
            got = b.last_incoming(c["conv"])
            if not got:
                continue
            message, first = got
            h = logic.msg_hash(c["conv"], message)
            if already(c["conv"], h):
                continue
            rule = logic.pick_rule(rs, message, first)
            if not rule:
                with store.db() as d:   # 当てはまるルールがない → 次からは見ない
                    d.execute("INSERT OR IGNORE INTO replied(conv, msg_hash, rule_id, at, dry) VALUES(?,?,?,?,1)", (c["conv"], h, None, time.time()))
                continue
            b.reply(c["conv"], rule["reply"], dry)
            with store.db() as d:
                d.execute("INSERT OR IGNORE INTO replied(conv, msg_hash, rule_id, at, dry) VALUES(?,?,?,?,?)",
                          (c["conv"], h, rule["id"], time.time(), 1 if dry else 0))
            done += 1
            store.log("info", "dm", f"ルール「{rule['name']}」で{'返信を下書きしました（送っていません）' if dry else '返信しました'}")
            time.sleep(random.uniform(20, 60))   # 続けて返さない
    store.update_status(last_dm_check=time.time(), dm_checked=len(convs), dm_replied=done)
    if manual or done:
        store.log("info", "dm", f"DMを確認しました：会話 {len(convs)}件を確認、{'下書き' if dry else '返信'} {done}件")


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
            elif req == "dm_probe":            # DM画面の作りを調べる（診断用・何も送らない）
                try:
                    with Browser(engine="iphone") as b0:      # iPhone 側のログイン情報を、パソコン（Chromium）側にも写す
                        ck = [c for c in b0.ctx.cookies(["https://x.com"])]
                    with Browser(engine="pc") as b:
                        b.ctx.clear_cookies()
                        b.ctx.add_cookies(ck)
                        ck = None
                        shots = []
                        for name, url in (("chat-pc", "https://x.com/i/chat"),):
                            path, info, shot = b.probe(url)
                            shots.append(shot)
                            top = sorted(info["testids"].items(), key=lambda kv: -kv[1])[:40]
                            print(f"[probe] {name}: url={path} rows={info['rows']} tabs={info['tabs']} hrefs={info['hrefs']}", flush=True)
                            print(f"[probe] {name}: testids={top}", flush=True)
                            print(f"[probe] {name}: main_keywords={info.get('kw_main')} detail={info.get('detail')} errors={info.get('errors')}", flush=True)
                            for fr in info.get("frames", []):
                                print(f"[probe] {name}: frame={fr}", flush=True)
                        store.update_status(snapshots=[x for x in shots if x], snapshot_at=time.time())
                    with Browser() as b2:   # パソコン版で開いたあとも、iPhone 側でログインが続いているか
                        print(f"[probe] after: iphone logged_in={b2.logged_in()}", flush=True)
                    store.log("info", "check", "DM画面を調べました（何も送っていません）")
                except Exception as e:  # noqa: BLE001
                    store.log("error", "check", f"DM画面を調べられませんでした: {e}")
            elif req == "check_dm":            # 管理サイトの「今すぐDMを確認」
                try:
                    run_dms(s, manual=True)
                except NotLoggedIn as e:
                    store.update_status(logged_in=False)
                    store.log("error", "login", str(e))
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
