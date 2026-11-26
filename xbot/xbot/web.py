"""管理サイト（iPhone の Safari で使う）。gunicorn で動かす: gunicorn xbot.web:app"""
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime

from flask import Flask, abort, redirect, render_template_string, request, send_from_directory, session

from . import cookies as xcookies, logic, store, xlogin

ADMIN_PASSWORD = os.environ.get("XBOT_ADMIN_PASSWORD", "")
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("XBOT_COOKIE_SECURE", "1") == "1", PERMANENT_SESSION_LIFETIME=7 * 86400)


def _secret_key():
    path = os.path.join(store.DATA_DIR, ".flask_secret")
    os.makedirs(store.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return open(path).read().strip()


app.secret_key = _secret_key()
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGES = 4
_fails = {}


def authed():
    return bool(session.get("ok"))


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def guard():
    if request.endpoint in ("login", "auth", "static", "health"):
        return None
    if not authed():
        return redirect("/login")
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    return None


def flash(msg):
    session["flash"] = msg


def back(tab):
    return redirect("/?tab=" + tab)


@app.get("/health")
def health():
    return "ok"


@app.get("/auth")
def auth():
    """Caddy の forward_auth 用：ログイン画面（noVNC）を管理者だけに見せる。"""
    return ("", 200) if authed() else ("", 401)


@app.route("/login", methods=["GET", "POST"])
def login():
    msg = ""
    if request.method == "POST":
        # Caddy が最後に本当の接続元を付け足すので、最後の値を使う（先頭は送る側が自由に書けてしまう）
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[-1].strip()
        fails = [t for t in _fails.get(ip, []) if t > time.time() - 900]
        if len(fails) >= 8:
            msg = "失敗が続いたため、15分ほど待ってからお試しください。"
        elif ADMIN_PASSWORD and hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        else:
            _fails[ip] = fails + [time.time()]
            msg = "パスワードが違います。" if ADMIN_PASSWORD else "管理パスワードが設定されていません（XBOT_ADMIN_PASSWORD）。"
    return render_template_string(PAGE, view="login", msg=msg, csrf="")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------------------- 画面
@app.get("/")
def index():
    tab = request.args.get("tab", "home")
    s = store.get_all()
    with store.db() as d:
        posts = [dict(r) for r in d.execute("SELECT * FROM posts ORDER BY CASE WHEN state IN ('scheduled','posting') THEN 0 ELSE 1 END, "
                                            "CASE WHEN state IN ('scheduled','posting') THEN at ELSE -at END LIMIT 100")]
        rules = [dict(r) for r in d.execute("SELECT * FROM rules ORDER BY priority, id")]
        events = [dict(r) for r in d.execute("SELECT * FROM events ORDER BY id DESC LIMIT 150")]
        today = d.execute("SELECT COUNT(*) c FROM posts WHERE state='posted' AND done_at>=?", (logic.jst_day_start(time.time()),)).fetchone()["c"]
        hour = d.execute("SELECT COUNT(*) c FROM replied WHERE dry=0 AND at>=?", (time.time() - 3600,)).fetchone()["c"]
        recent = [dict(r) for r in d.execute("SELECT r.*, u.name FROM replied r LEFT JOIN rules u ON u.id=r.rule_id WHERE r.rule_id IS NOT NULL ORDER BY r.at DESC LIMIT 50")]
    for p in posts:
        p["rep"] = logic.describe_repeat(json.loads(p["repeat"])) if p.get("repeat") else ""
        p["imgs"] = json.loads(p["images"] or "[]")
        p["when"] = logic.fmt_jst(p["at"])
        p["at_local"] = datetime.fromtimestamp(p["at"], logic.JST).strftime("%Y-%m-%dT%H:%M")
        p["editable"] = p["state"] in EDITABLE and not p.get("parent")
        p["done"] = logic.fmt_jst(p["done_at"])
    for e in events:
        e["when"] = logic.fmt_jst(e["at"])
    for r in recent:
        r["when"] = logic.fmt_jst(r["at"])
    st = s.get("status") or {}
    alive = time.time() - float(st.get("heartbeat") or 0) < 90
    return render_template_string(
        PAGE, view="main", tab=tab, s=s, st=st, alive=alive, posts=posts, rules=rules, events=events, recent=recent,
        today=today, hour=hour, kinds=logic.KINDS, csrf=csrf(), msg=session.pop("flash", ""), fmt=logic.fmt_jst,
        login_open=time.time() < float(s["login_until"] or 0), now_local=time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 600)),
        max_len=logic.MAX_POST_LEN, warp_ok=_warp_ok())


def _warp_ok():
    from .browser import warp_ready
    try:
        return warp_ready()
    except Exception:  # noqa: BLE001
        return False


@app.get("/img/<name>")
def img(name):
    return send_from_directory(store.UPLOAD_DIR, os.path.basename(name))


@app.get("/shot/<name>")
def shot(name):
    return send_from_directory(store.SHOT_DIR, os.path.basename(name))


@app.get("/login-screen")
def login_screen():
    s = store.get_all()
    if time.time() > float(s["login_until"] or 0) or not s.get("vnc_password"):
        flash("ログイン画面は閉じています。")
        return back("home")
    return render_template_string(PAGE, view="vnc", pw=s["vnc_password"], until=logic.fmt_jst(s["login_until"]), csrf=csrf())


# ---------------------------------------------------------------------- X へのログイン（入力した内容でサーバーがログイン）
@app.get("/xlogin")
def xlogin_page():
    return redirect("/xcookie")   # 自動入力ログインはやめた（X に止められるため）


@app.route("/xcookie", methods=["GET", "POST"])
def xcookie():
    err = None
    if request.method == "POST":
        try:
            ck = xcookies.parse(request.form.get("json", ""), request.form.get("auth_token", ""), request.form.get("ct0", ""))
            ua = request.headers.get("User-Agent", "")[:400]
            if "iPhone" in ua:          # 貼り付けた iPhone のブラウザ名に合わせる（Orion で開いていれば同じになる）
                store.put("ua", ua)
            xlogin.call("cookies", ck)
            ck = None   # 入力内容はここで手放す（サーバーのブラウザの中だけに残る）
            return redirect("/xcookie/status")
        except ValueError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = f"常駐プロセスにつながりませんでした（{type(e).__name__}）。少し待ってからもう一度お試しください。"
    return render_template_string(PAGE, view="xcookie", err=err, csrf=csrf())


@app.get("/xcookie/status")
def xcookie_status():
    try:
        st = xlogin.call("cookie_status")
    except Exception as e:  # noqa: BLE001
        st = {"state": "failed", "step": 4, "label": f"常駐プロセスにつながりませんでした（{type(e).__name__}）。"}
    return render_template_string(PAGE, view="xstatus", st=st, steps=xlogin.STEPS, csrf=csrf(), title="ログイン情報の取り込み", again="/xcookie")


@app.route("/chatpin", methods=["GET", "POST"])
def chatpin():
    err = None
    if request.method == "POST":
        pin = (request.form.get("pin") or "").strip()
        if not (pin.isascii() and pin.isdigit() and len(pin) == 4):
            err = "暗証番号は4桁の数字で入れてください。"
        else:
            try:
                xlogin.call("pin", pin)
                pin = None
                return redirect("/chatpin/status")
            except Exception as e:  # noqa: BLE001
                err = f"常駐プロセスにつながりませんでした（{type(e).__name__}）。"
    return render_template_string(PAGE, view="chatpin", err=err, csrf=csrf())


@app.get("/chatpin/status")
def chatpin_status():
    try:
        st = xlogin.call("pin_status")
    except Exception as e:  # noqa: BLE001
        st = {"state": "failed", "step": 4, "label": f"常駐プロセスにつながりませんでした（{type(e).__name__}）。"}
    return render_template_string(PAGE, view="xstatus", st=st, steps=st.get("steps") or xlogin.PIN_STEPS, csrf=csrf(),
                                  title="チャットの暗証番号", again="/chatpin")


@app.post("/xlogin/cancel")
def xlogin_cancel():
    try:
        xlogin.call("cancel")
    except Exception:  # noqa: BLE001
        pass
    flash("ログインをやめました。")
    return back("home")


# ---------------------------------------------------------------------- 予約投稿
@app.post("/post/new")
def post_new():
    text = (request.form.get("text") or "").strip()
    files = [f for f in request.files.getlist("images") if f and f.filename]
    if not text and not files:
        flash("本文か画像を入れてください。")
        return back("posts")
    if logic.post_length(text) > logic.MAX_POST_LEN:
        flash(f"本文が長すぎます（全角は2文字として {logic.MAX_POST_LEN} まで）。")
        return back("posts")
    if len(files) > MAX_IMAGES:
        flash(f"画像は{MAX_IMAGES}枚までです。")
        return back("posts")
    try:
        at = logic.parse_jst(request.form.get("at") or "") if request.form.get("at") else time.time()
    except ValueError:
        flash("日時の形が正しくありません。")
        return back("posts")
    rep = None
    rtype = request.form.get("repeat", "none")
    if rtype in ("daily", "weekly", "hours"):
        hm = datetime.fromtimestamp(at, logic.JST).strftime("%H:%M")
        rep = {"type": rtype, "time": hm}
        if rtype == "weekly":
            days = sorted({int(x) for x in request.form.getlist("days") if x.isdigit() and 0 <= int(x) <= 6})
            if not days:
                flash("毎週の場合は、曜日を1つ以上選んでください。")
                return back("posts")
            rep["days"] = days
        if rtype == "hours":
            h = (request.form.get("every_hours") or "").strip()
            if not h.isdigit() or not 1 <= int(h) <= 168:
                flash("「〇時間ごと」は 1〜168 の数字で入れてください。")
                return back("posts")
            rep["hours"] = int(h)
            rep["anchor"] = at
        if at < time.time() - 60:
            at = logic.next_time(rep, time.time())
        elif rtype == "weekly" and datetime.fromtimestamp(at, logic.JST).weekday() not in rep["days"]:
            at = logic.next_time(rep, at)
    os.makedirs(store.UPLOAD_DIR, exist_ok=True)
    names = []
    for f in files:
        ext = os.path.splitext(f.filename.lower())[1]
        if ext == ".heic":
            flash("HEIC形式の画像は使えません。iPhoneの「設定 → カメラ → フォーマット → 互換性優先」にするか、スクショで送ってください。")
            return back("posts")
        if ext not in IMG_EXT:
            flash("画像は JPG / PNG / GIF / WEBP にしてください。")
            return back("posts")
        name = uuid.uuid4().hex + ext
        f.save(os.path.join(store.UPLOAD_DIR, name))
        names.append(name)
    with store.db() as d:
        d.execute("INSERT INTO posts(text, images, at, created, repeat) VALUES(?,?,?,?,?)",
                  (text, json.dumps(names), at, time.time(), json.dumps(rep, ensure_ascii=False) if rep else None))
    flash(f"予約しました（{'定期：' + logic.describe_repeat(rep) + '・初回 ' if rep else ''}{logic.fmt_jst(at)}）。" + ("いまは下書きモードなので、実際には投稿されません。" if store.get("dry_run") else ""))
    return back("posts")


EDITABLE = ("scheduled", "canceled", "failed", "dry")   # 投稿の最中（posting）と投稿済みは編集できない


@app.post("/post/<int:pid>/edit")
def post_edit(pid):
    """予約の編集：本文・日時・画像を外す。状態（予約中／取り消し など）は変えない。"""
    text = (request.form.get("text") or "").strip()
    drop = set(request.form.getlist("drop"))
    with store.db() as d:
        row = d.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    if row["state"] not in EDITABLE or row["parent"]:
        flash("この投稿は編集できません（投稿の最中・投稿済み・定期の記録）。")
        return back("posts")
    images = [n for n in json.loads(row["images"] or "[]") if n not in drop]
    if not text and not images:
        flash("本文か画像のどちらかは残してください。")
        return back("posts")
    if logic.post_length(text) > logic.MAX_POST_LEN:
        flash(f"本文が長すぎます（全角は2文字として {logic.MAX_POST_LEN} まで）。")
        return back("posts")
    at, rep = row["at"], json.loads(row["repeat"]) if row["repeat"] else None
    if request.form.get("at"):
        try:
            new_at = logic.parse_jst(request.form["at"])
        except ValueError:
            flash("日時の形が正しくありません。")
            return back("posts")
        if abs(new_at - at) >= 60:                 # 日時を変えたときだけ（分より細かい違いは、変えていないとみなす）
            at = new_at
            if rep:                                # 定期：時刻（と「〇時間ごと」の起点）も合わせ、次の回を計算し直す
                rep["time"] = datetime.fromtimestamp(at, logic.JST).strftime("%H:%M")
                if rep["type"] == "hours":
                    rep["anchor"] = at
                if at < time.time() - 60 or (rep["type"] == "weekly" and datetime.fromtimestamp(at, logic.JST).weekday() not in rep["days"]):
                    at = logic.next_time(rep, max(time.time(), at)) or at
    with store.db() as d:
        n = d.execute(f"UPDATE posts SET text=?, images=?, at=?, repeat=?, error=NULL WHERE id=? AND state IN ({','.join('?' * len(EDITABLE))})",
                      (text, json.dumps(images), at, json.dumps(rep, ensure_ascii=False) if rep else None, pid, *EDITABLE)).rowcount
        others = " ".join(r["images"] for r in d.execute("SELECT images FROM posts")) if n else ""
    if not n:
        flash("保存できませんでした（ちょうど投稿が始まったところです）。")
        return back("posts")
    for name in drop & set(json.loads(row["images"] or "[]")):   # 外した画像のファイルを消す（ほかの予約が使っていなければ）
        if name not in others:
            try:
                os.remove(os.path.join(store.UPLOAD_DIR, os.path.basename(name)))
            except OSError:
                pass
    flash(f"予約を保存しました（{logic.fmt_jst(at)}）。")
    return back("posts")


@app.post("/post/<int:pid>/<op>")
def post_op(pid, op):
    with store.db() as d:
        row = d.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not row:
            abort(404)
        if op == "cancel" and row["state"] == "scheduled":
            d.execute("UPDATE posts SET state='canceled' WHERE id=?", (pid,))
            flash("予約を止めました。" + ("（定期投稿は「もう一度予約」で再開できます）" if row["repeat"] else ""))
        elif op == "retry" and row["state"] in ("failed", "canceled", "dry"):
            at = max(time.time(), row["at"])
            if row["repeat"]:
                at = logic.next_time(json.loads(row["repeat"]), time.time()) or at
            d.execute("UPDATE posts SET state='scheduled', tries=0, error=NULL, at=? WHERE id=?", (at, pid))
            flash("もう一度予約しました。")
        elif op == "delete" and row["state"] != "posting":
            others = " ".join(r["images"] for r in d.execute("SELECT images FROM posts WHERE id!=?", (pid,)))
            for n in json.loads(row["images"] or "[]"):
                if n in others:
                    continue
                try:
                    os.remove(os.path.join(store.UPLOAD_DIR, n))
                except OSError:
                    pass
            d.execute("DELETE FROM posts WHERE id=?", (pid,))
            flash("削除しました（Xに投稿済みのものは、X上では消えません）。")
    return back("posts")


# ---------------------------------------------------------------------- DM返信ルール
def _rule_form():
    f = request.form
    kind = f.get("kind", "keyword")
    if kind not in logic.KINDS:
        raise ValueError("条件の種類が正しくありません。")
    name = (f.get("name") or "").strip()[:60] or logic.KINDS[kind]
    reply = (f.get("reply") or "").strip()
    if not reply:
        raise ValueError("返信する文を入れてください。")
    if len(reply) > 2000:
        raise ValueError("返信する文は2000文字までです。")
    kw = (f.get("keywords") or "").strip()[:500]
    if kind == "keyword" and not logic.keywords_of({"keywords": kw}):
        raise ValueError("キーワードを1つ以上入れてください。")
    pr = (f.get("priority") or "100").strip()
    if not pr.isdigit():
        raise ValueError("優先度は数字で入れてください。")
    return dict(name=name, kind=kind, keywords=kw, match="all" if f.get("match") == "all" else "any", reply=reply, priority=int(pr))


@app.post("/rule/new")
def rule_new():
    try:
        r = _rule_form()
    except ValueError as e:
        flash(str(e))
        return back("dm")
    with store.db() as d:
        d.execute("INSERT INTO rules(name, kind, keywords, match, reply, priority, created) VALUES(?,?,?,?,?,?,?)",
                  (r["name"], r["kind"], r["keywords"], r["match"], r["reply"], r["priority"], time.time()))
    store.put("rules_changed", time.time())
    flash(f"ルール「{r['name']}」を追加しました。")
    return back("dm")


@app.post("/rule/<int:rid>/<op>")
def rule_op(rid, op):
    store.put("rules_changed", time.time())   # ルールを変えたら、当てはまらなかったメッセージをもう一度見る
    with store.db() as d:
        if op == "toggle":
            d.execute("UPDATE rules SET enabled=1-enabled WHERE id=?", (rid,))
            flash("ルールのオン・オフを切り替えました。")
        elif op == "delete":
            d.execute("DELETE FROM rules WHERE id=?", (rid,))
            flash("ルールを削除しました。")
        elif op == "edit":
            try:
                r = _rule_form()
            except ValueError as e:
                flash(str(e))
                return back("dm")
            d.execute("UPDATE rules SET name=?, kind=?, keywords=?, match=?, reply=?, priority=? WHERE id=?",
                      (r["name"], r["kind"], r["keywords"], r["match"], r["reply"], r["priority"], rid))
            flash("ルールを保存しました。")
    return back("dm")


# ---------------------------------------------------------------------- 設定・操作
LIMIT_NAMES = {"max_posts_day": "1日の投稿数の上限", "max_replies_hour": "1時間の返信数の上限", "dm_interval_min": "DMを確認する間隔",
               "reply_cooldown_hours": "同じ相手に続けて返信しない時間", "dm_scan_limit": "1回に確認する会話の数"}
LIMITS = {"max_posts_day": (1, 50), "max_replies_hour": (1, 60), "dm_interval_min": (2, 120), "reply_cooldown_hours": (0, 720), "dm_scan_limit": (1, 30)}


@app.post("/settings")
def settings():
    for k, (lo, hi) in LIMITS.items():
        v = (request.form.get(k) or "").strip()
        if not v.isdigit() or not lo <= int(v) <= hi:
            flash(f"「{LIMIT_NAMES.get(k, k)}」は {lo}〜{hi} の数字で入れてください。")
            return back("settings")
        store.put(k, int(v))
    flash("設定を保存しました。")
    return back("settings")


@app.post("/settings/browser")
def settings_browser():
    eng = request.form.get("engine")
    net = request.form.get("network")
    if eng in ("iphone", "pc"):
        store.put("engine", eng)
    if net in ("warp", "direct"):
        store.put("network", net)
    flash("ブラウザの設定を保存しました。ログイン情報は、ブラウザの種類ごとに別々です（切り替えたら貼り付け直してください）。")
    return back("settings")


@app.post("/action/<op>")
def action(op):
    tab = "home"
    if op == "check_dm" and request.form.get("back") not in (None, "", "home", "dm"):
        abort(400)
    if op == "pause":
        store.put("paused", True); flash("一時停止しました。予約投稿もDM返信も止まります。")
    elif op == "resume":
        store.put("paused", False); flash("再開しました。")
    elif op == "dry_on":
        store.put("dry_run", True); flash("下書きモードにしました。投稿・送信ボタンは押しません。")
    elif op == "dry_off":
        store.put("dry_run", False); flash("本番モードにしました。予約どおり実際に投稿・返信します。")
    elif op == "dm_on":
        store.put("dm_enabled", True); flash("DM自動返信をオンにしました。"); tab = "dm"
    elif op == "dm_off":
        store.put("dm_enabled", False); flash("DM自動返信をオフにしました。"); tab = "dm"
    elif op == "req_on":
        store.put("dm_accept_requests", True); flash("メッセージリクエストを自動で許可します。"); tab = "dm"
    elif op == "req_off":
        store.put("dm_accept_requests", False); flash("メッセージリクエストの自動許可をやめました。"); tab = "dm"
    elif op in ("check_login", "snapshot"):
        store.put("request", op); flash("確認を依頼しました。1分ほどで結果が出ます（画面を再読み込みしてください）。")
    elif op == "check_dm":
        store.put("request", "check_dm"); tab = request.form.get("back") or "dm"
        flash("DMの確認を始めました。1〜2分で結果が出ます（画面を再読み込みしてください）。"
              + ("いまは下書きモードなので、返信は送らずに下書きだけ確認します。" if store.get("dry_run") else ""))
    elif op == "login_start":
        store.put("vnc_password", secrets.token_hex(4))
        store.put("login_until", time.time() + 15 * 60)
        flash("ログイン画面を準備しています。10秒ほどしてから「ログイン画面を開く」を押してください。")
    elif op == "login_stop":
        store.put("request", "stop_login"); store.put("login_until", 0); flash("ログイン画面を閉じます。")
    return back(tab)


PAGE = open(os.path.join(os.path.dirname(__file__), "page.html"), encoding="utf-8").read()
