"""X へのログインを、管理サイトから入力した内容でサーバー側のブラウザが行う。
パスワードなどはメモリ上で使うだけで、ファイル・データベースには保存しない。
常駐プロセス（worker）の中で動き、管理サイトとは 127.0.0.1 の小さな窓口でやり取りする。"""
import base64
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store

PORT = int(os.environ.get("XBOT_LOGIN_PORT", "8091"))
TOKEN_FILE = os.path.join(store.DATA_DIR, ".login_token")
LOGIN_URL = "https://x.com/i/flow/login"
IDLE_LIMIT = 10 * 60          # 10分なにもしなければ閉じる
DISPLAY = ":98"


def token():
    os.makedirs(store.DATA_DIR, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_hex(24))
        os.chmod(TOKEN_FILE, 0o600)
    return open(TOKEN_FILE).read().strip()


class Session:
    """ブラウザを専用のスレッドで持ち、管理サイトからの入力を1つずつ処理する。"""

    def __init__(self):
        self.q = queue.Queue()
        self.result = {}
        self.done = threading.Event()
        self.last = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.alive = True
        self.thread.start()

    # ---- 管理サイトから呼ばれる（別スレッド）
    def call(self, op, value=None, timeout=60):
        ev = threading.Event()
        box = {}
        self.q.put((op, value, ev, box))
        if not ev.wait(timeout):
            return {"state": "busy", "label": "処理に時間がかかっています。少し待ってから「画面を更新」を押してください。"}
        return box.get("res") or {}

    # ---- ブラウザのスレッド
    def _run(self):
        from .browser import Browser, SEL
        xvfb = None
        b = None
        try:
            display = None
            if shutil.which("Xvfb"):     # 画面ありのブラウザの方が、ログイン時に止められにくい
                xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1100x900x24", "-nolisten", "tcp"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.5)
                display = DISPLAY
            b = Browser(headless=display is None, display=display).__enter__()
            b.page.set_viewport_size({"width": 1100, "height": 900})
            b.page.goto(LOGIN_URL, wait_until="domcontentloaded")
            time.sleep(4)
            while self.alive:
                try:
                    op, value, ev, box = self.q.get(timeout=5)
                except queue.Empty:
                    if time.time() - self.last > IDLE_LIMIT:
                        store.log("info", "login", "10分操作がなかったので、ログインの画面を閉じました")
                        break
                    continue
                self.last = time.time()
                try:
                    if op == "input":
                        self._type(b, value)
                    elif op == "cancel":
                        box["res"] = {"state": "closed"}
                        ev.set()
                        break
                    res = self._state(b, SEL)
                except Exception as e:  # noqa: BLE001
                    res = {"state": "error", "label": f"エラー：{str(e)[:200]}", "shot": self._shot(b)}
                box["res"] = res
                ev.set()
                if res.get("state") == "done":
                    store.update_status(logged_in=True, login_checked=time.time())
                    store.log("info", "login", "Xにログインしました（ログイン状態を保存しました）")
                    time.sleep(3)   # ログイン状態をプロフィールに書き込む時間
                    break
        except Exception as e:  # noqa: BLE001
            store.log("error", "login", f"ログインの画面を開けませんでした: {e}")
            self.result = {"state": "error", "label": f"ブラウザを起動できませんでした：{e}"}
        finally:
            self.alive = False
            # 残っている依頼には「閉じた」と返す
            while not self.q.empty():
                try:
                    _, _, ev, box = self.q.get_nowait()
                    box["res"] = {"state": "closed"}
                    ev.set()
                except queue.Empty:
                    break
            if b:
                try:
                    b.__exit__(None, None, None)
                except Exception:
                    pass
            if xvfb:
                xvfb.terminate()
            self.done.set()

    @staticmethod
    def _shot(b):
        try:
            return "data:image/jpeg;base64," + base64.b64encode(b.page.screenshot(type="jpeg", quality=60)).decode()
        except Exception:
            return ""

    def _visible(self, b, sel):
        el = b.page.query_selector(sel)
        try:
            return el if el and el.is_visible() else None
        except Exception:
            return None

    def _field(self, b, allow_filled=False):
        """いま入力を求められている欄と、その種類（まだ空の欄を、確認コード→ユーザー名→パスワードの順に探す）。"""
        for sel, kind in (('input[data-testid="ocfEnterTextTextInput"]', "text"),
                          ('input[name="username_or_email"]', "username"), ('input[autocomplete~="username"]', "username"),
                          ('input[name="text"]', "username"),
                          ('input[name="password"], input[type="password"]', "password"),
                          ('input[inputmode="numeric"]', "text")):
            for el in b.page.query_selector_all(sel):
                try:
                    usable = el.evaluate("e => !e.closest('[inert]') && !e.closest('[aria-hidden=\"true\"]')")
                    if usable and el.is_visible() and el.is_enabled() and (allow_filled or not (el.input_value() or "")):
                        return el, kind
                except Exception:
                    continue
        return None, None

    def _state(self, b, SEL):
        p = b.page
        time.sleep(1)
        if self._visible(b, SEL["logged_in"]) or "/home" in p.url:
            return {"state": "done", "label": "ログインできました。", "shot": self._shot(b)}
        el, kind = self._field(b)
        heading = ""
        for sel in ('h1', '[role="heading"]'):
            h = self._visible(b, sel)
            if h:
                heading = (h.inner_text() or "").strip()[:80]
                break
        alert = ""
        try:
            box = p.query_selector('[role="dialog"]') or p.query_selector("body")
            for line in (box.inner_text() or "").splitlines():
                line = line.strip()
                if line and any(k in line for k in ("見つかりません", "正しくありません", "間違", "エラー", "できません", "もう一度", "無効", "制限",
                                                     "incorrect", "wrong", "not find", "try again")):
                    alert = line[:140]
                    break
        except Exception:
            pass
        if not el and alert:          # 間違いがあった → 同じ欄にもう一度入れてもらう
            el, kind = self._field(b, allow_filled=True)
        if alert:
            heading = alert
        if not el:
            return {"state": "wait", "label": heading or "画面を読み込み中です。少し待ってから「画面を更新」を押してください。", "shot": self._shot(b)}
        labels = {"password": "パスワード", "username": "電話番号・メールアドレス・ユーザー名", "text": heading or "求められている内容（確認コードなど）"}
        return {"state": "ask", "kind": kind, "label": labels[kind], "heading": heading, "shot": self._shot(b)}

    def _type(self, b, value):
        el, kind = self._field(b)
        if not el:
            el, kind = self._field(b, allow_filled=True)   # 間違いを直して入れ直すとき
        if not el:
            return
        el.click()
        el.fill("")
        b.page.keyboard.type(str(value or ""), delay=60)
        time.sleep(0.6)
        nxt, _ = self._field(b)
        if kind == "username" and nxt is not None:
            return          # 同じ画面にパスワード欄がある → パスワードを入れてから送る
        self._submit(b)
        time.sleep(4)

    def _submit(self, b):
        """「次へ」「続ける」「ログイン」を押す（なければ Enter）。"""
        p = b.page
        for name in ("ログイン", "次へ", "続ける", "Log in", "Next", "Continue"):
            for btn in p.query_selector_all('button, [role="button"]'):
                try:
                    if (btn.is_visible() and btn.is_enabled() and (btn.inner_text() or "").strip() == name
                            and btn.evaluate("e => !e.closest('[inert]')")):
                        btn.click()
                        return
                except Exception:
                    continue
        p.keyboard.press("Enter")


STEPS = ["受け付けました", "サーバーのブラウザを起動しています", "x.com を開いて、ログインできているか確認しています", "結果"]


class Importer:
    """Cookie の取り込みを裏で行い、いまどの段階かを返す。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.st = {"state": "idle"}

    def running(self):
        return self.st.get("state") == "running"

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def start(self, cookies):
        if self.running():
            return dict(self.st)
        self.st = {"state": "running", "step": 1, "label": STEPS[0], "started": time.time()}
        threading.Thread(target=self._run, args=(cookies,), daemon=True).start()
        return dict(self.st)

    def status(self):
        with self.lock:
            st = dict(self.st)
        if st.get("started"):
            st["elapsed"] = int(time.time() - st["started"])
        return st

    def _run(self, cookies):
        from .browser import Browser
        try:
            if MANAGER.active_session():      # 前の「自動入力ログイン」のブラウザが残っていたら閉じる
                MANAGER.handle("cancel", None)
            self._set(step=2, label=STEPS[1])
            with Browser() as b:
                b.ctx.clear_cookies()
                b.ctx.add_cookies(cookies)
                cookies = None
                self._set(step=3, label=STEPS[2])
                ok = b.logged_in()
                time.sleep(2)
                shot = Session._shot(b)
            store.update_status(logged_in=ok, login_checked=time.time())
            store.log("info" if ok else "warn", "login", "ログイン情報を取り込みました：" + ("Xにログインできています" if ok else "ログインできていません"))
            self._set(state="done" if ok else "failed", step=4, shot=shot,
                      label="ログインできました。予約投稿・DM返信が使えます。" if ok else
                      "ログインできませんでした。値が古いか、コピーが途中で切れている可能性があります。Orion で x.com を開き直してから、もう一度コピーして貼ってください。")
        except Exception as e:  # noqa: BLE001
            store.log("error", "login", f"ログイン情報を取り込めませんでした: {e}")
            self._set(state="failed", step=4, label=f"うまくいきませんでした：{str(e)[:200]}")


IMPORTER = Importer()


class Manager:
    def __init__(self):
        self.s = None
        self.lock = threading.Lock()

    def active_session(self):
        return bool(self.s and self.s.alive)

    def active(self):
        return self.active_session() or IMPORTER.running()

    def handle(self, op, value):
        if op == "cookies":
            return IMPORTER.start(value or [])
        if op == "cookie_status":
            return IMPORTER.status()
        with self.lock:
            if op == "start":
                if not self.active_session():
                    self.s = Session()
                return self.s.call("state", timeout=90)
            if not self.active_session():
                return {"state": "closed", "label": "ログインの画面は閉じています。"}
            res = self.s.call(op, value, timeout=90)
            if op == "cancel":
                self.s.done.wait(30)
            return res


MANAGER = Manager()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # 入力内容をログに出さない
        pass

    def do_POST(self):
        if self.headers.get("X-Token") != token():
            self.send_response(403)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        op = body.get("op")
        if op not in ("cancel", "cookies", "cookie_status"):   # 自動入力ログインは使わない（X に止められるため）
            self.send_response(400)
            self.end_headers()
            return
        res = MANAGER.handle(op, body.get("value"))
        body = None   # 入力内容をすぐ手放す
        data = json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve():
    token()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(op, value=None):
    """管理サイト側から使う。"""
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/", data=json.dumps({"op": op, "value": value}).encode(),
                                 headers={"X-Token": token(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=100) as r:
        return json.loads(r.read())
