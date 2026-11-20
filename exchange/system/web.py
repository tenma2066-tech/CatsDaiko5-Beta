"""換金管理サイト。単一プロセス・単一イベントループで取引と復旧処理を共有する。"""
import asyncio
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from jinja2 import Environment, FileSystemLoader

import aml
import bot as exchange
import store

ROOT = Path(__file__).resolve().parent
TEMPLATE = Environment(loader=FileSystemLoader(ROOT), autoescape=True).get_template("web.html")
COOKIE = "__Host-paykyash"
OWNER_SESSION = web.RequestKey("owner", dict)
OWNER = -1  # Web 専用の単一所有者。旧 Discord アカウント番号とは重ならない。
LIFETIME = 8 * 3600


class Owner:
    id = OWNER
    mention = "管理者"

    def __str__(self):
        return "管理者"


class WebReply:
    """既存の取引関数が返す進行状況を、その要求を送った管理者に返す。"""
    user = Owner()

    def __init__(self):
        self.followup = self
        self.messages = []

    async def send(self, content=None, *, embed=None, **kwargs):
        if embed:
            self.messages.append(embed.title or "処理結果")
        elif content:
            self.messages.append(content.removeprefix("-# "))


def connections():
    return {"paypay": bool((exchange.paypay.account_info() or {}).get("access_token")),
            "kyash": exchange.kyash.ready()}


def link_for(service, value):
    url = urlsplit(value)
    host = {"paypay": "pay.paypay.ne.jp", "kyash": "kyash.me"}[service]
    if len(value) > 512 or url.scheme != "https" or url.netloc != host or not url.path.strip("/"):
        raise ValueError(f"{host} の送金リンクを入力してください。")
    if service == "kyash" and not url.path.startswith("/payments/"):
        raise ValueError("Kyash の送金リンクを入力してください。")
    return value


def make_app(password_file):
    sessions = {}
    failures = {}
    busy = asyncio.Lock()
    auth_file = Path(password_file)

    def session_for(request):
        sid = request.cookies.get(COOKIE, "")
        current = sessions.get(sid)
        if (current and current["expires"] > time.monotonic() and auth_file.is_file()
                and current["credential"] == auth_file.stat().st_mtime_ns):
            return current
        sessions.pop(sid, None)
        return None

    @web.middleware
    async def protect(request, handler):
        public = request.path in ("/login", "/health", "/style.css")
        current = session_for(request)
        if not public and not current:
            raise web.HTTPSeeOther("/login")
        request[OWNER_SESSION] = current
        if request.method == "POST":
            if request.content_type != "application/x-www-form-urlencoded":
                raise web.HTTPUnsupportedMediaType()
            if request.headers.get("Origin") != "https://" + request.host:
                raise web.HTTPForbidden(text="送信元を確認できません。")
            if current and request.path != "/login":
                form = await request.post()
                if not hmac.compare_digest(str(form.get("csrf", "")).encode(), current["csrf"].encode()):
                    raise web.HTTPForbidden(text="画面を再読み込みしてください。")
        return await handler(request)

    app = web.Application(middlewares=[protect], client_max_size=8192)

    async def headers(request, response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    app.on_response_prepare.append(headers)

    def render(page, **data):
        return web.Response(text=TEMPLATE.render(page=page, **data), content_type="text/html")

    async def login(request):
        if request.method == "GET":
            if request[OWNER_SESSION]:
                raise web.HTTPSeeOther("/")
            return render("login", configured=auth_file.is_file())
        if not auth_file.is_file():
            return web.Response(status=503, text="管理パスワードの設定待ちです。")
        now = time.monotonic()
        ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[-1].strip()
        for addr in list(failures):
            if failures[addr][0] <= now:
                del failures[addr]
        until, count = failures.get(ip, (now + 900, 0))
        if count >= 8 or len(failures) >= 1024:
            return web.Response(status=429, text="しばらく待ってからお試しください。")
        form = await request.post()
        with auth_file.open() as credential:
            generation = os.fstat(credential.fileno()).st_mtime_ns
            salt, expected = credential.read().strip().split(":")
        failures[ip] = (until, count + 1)
        candidate = await asyncio.to_thread(hash_password, form.get("password", "").encode(), bytes.fromhex(salt))
        if not hmac.compare_digest(candidate, expected):
            return render("login", configured=True, message="パスワードを確認してください。")
        failures.pop(ip, None)
        for sid in list(sessions):
            if sessions[sid]["expires"] <= now:
                del sessions[sid]
        if len(sessions) >= 16:
            del sessions[next(iter(sessions))]
        sid = secrets.token_urlsafe(32)
        sessions[sid] = {"csrf": secrets.token_urlsafe(), "expires": now + LIFETIME, "credential": generation}
        response = web.HTTPSeeOther("/")
        response.set_cookie(COOKIE, sid, secure=True, httponly=True, samesite="Strict", path="/", max_age=LIFETIME)
        raise response

    async def logout(request):
        sessions.pop(request.cookies[COOKIE], None)
        response = web.HTTPSeeOther("/login")
        response.del_cookie(COOKIE, path="/", secure=True)
        raise response

    async def dashboard(request):
        current = request[OWNER_SESSION]
        page = request.match_info.get("page", "overview")
        cfg = store.load_config()
        orders = store.recent_orders(100)
        flags = connections()
        return render(page, session=current, config=cfg, services=store.SERVICE_LABEL,
                      directions=store.DIRECTION_LABEL, connections=flags,
                      ready=all(flags.values()) and aml.gate(OWNER)["state"] == "ok" and cfg["enabled"],
                      bindings={s: store.binding(OWNER, s) for s in flags},
                      challenges={s: aml.get_challenge(OWNER, s) for s in flags},
                      orders=orders, states=exchange.STATUS_JP, stats=store.stats(),
                      holds=store.holds_in("WATCHING", "MANUAL", "RECEIVING"),
                      message=current.pop("message", ""), yen=exchange.yen,
                      quote=current.get("quote"), kyash_auth_pending=bool(current.get("kyash_auth")))

    async def submit(request):
        form = await request.post()
        current = request[OWNER_SESSION]
        action = request.match_info["action"]
        target = "/connections" if action in ("challenge", "register", "kyash-login", "kyash-verify") else "/"
        if action in ("confirm", "retry"):
            target = "/history"
        if busy.locked():
            raise web.HTTPConflict(text="別の操作を処理しています。完了を待ってください。")
        async with busy:
            reply = WebReply()
            cfg = store.load_config()
            svc = form.get("service", "")
            direction = form.get("direction", "")
            try:
                if action in ("estimate", "exchange") and direction not in store.DIRECTIONS:
                    raise ValueError("換金方向を選択してください。")
                if action in ("challenge", "register"):
                    if svc not in store.SERVICES or not connections()[svc]:
                        raise ValueError("先に決済サービスを接続してください。")
                if action == "estimate":
                    raw = form.get("amount", "")
                    if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 9 or int(raw) <= 0:
                        raise ValueError("金額は正の整数で入力してください。")
                    current["quote"] = {**exchange.quote(cfg, direction, int(raw)), "direction": direction}
                elif action == "exchange":
                    if not all(connections().values()) or aml.gate(OWNER)["state"] != "ok":
                        raise ValueError("決済サービスの接続と送金元の登録が必要です。")
                    if form.get("confirm") != "yes":
                        raise ValueError("送金リンクの受取と払い出しを確認してください。")
                    svc = "paypay" if direction == "p2k" else "kyash"
                    link = link_for(svc, form.get("link", "").strip())
                    if direction == "p2k":
                        await exchange.process_p2k(reply, link, form.get("passcode") or None)
                    else:
                        await exchange.process_k2p(reply, link)
                elif action == "challenge":
                    result = aml.issue_challenge(cfg, OWNER, svc)
                    if not result["ok"]:
                        raise ValueError("確認の発行間隔を空けてください。")
                elif action == "register":
                    link = link_for(svc, form.get("link", "").strip())
                    if svc == "paypay":
                        await exchange.process_reg_paypay(reply, link, form.get("passcode") or None)
                    else:
                        await exchange.process_reg_kyash(reply, link)
                elif action == "kyash-login":
                    current.pop("kyash_auth", None)
                    result = await asyncio.to_thread(exchange.kyash.login_step1, form.get("email", ""), form.get("password", ""))
                    if result["status"] == "link_required":
                        current["kyash_auth"] = result
                        reply.messages.append("Kyash から届いた認証リンクを入力してください。")
                    elif result["status"] != "ok":
                        raise ValueError("Kyash のログイン情報を確認してください。")
                elif action == "kyash-verify":
                    state = current.pop("kyash_auth", None)
                    result = await asyncio.to_thread(exchange.kyash.login_step2, state, form.get("verification", ""))
                    if result["status"] != "ok":
                        raise ValueError("認証リンクを確認して、もう一度ログインしてください。")
                elif action in ("retry", "confirm"):
                    order = store.get_order(form.get("order", ""))
                    if not order or order["user_id"] != OWNER or form.get("confirm") != "yes":
                        raise ValueError("注文と実行内容を確認してください。")
                    if action == "retry":
                        if order["status"] not in ("PAID", "FAILED", "HOLD") or (order["status"] == "HOLD" and order.get("hold_code") != "REVIEW"):
                            raise ValueError("この注文は払い出しできません。")
                        result = await exchange.payout(order["id"], reply.send, by=OWNER)
                        reply.messages.append(exchange.STATUS_JP.get(result["status"].upper(), result["status"]))
                    else:
                        if order["status"] != "UNCERTAIN" or form.get("result") not in ("sent", "unsent"):
                            raise ValueError("送金結果を確認してください。")
                        returning = order.get("pending_action") == "return"
                        sent = form["result"] == "sent"
                        status = ("RETURNED" if returning else "SENT") if sent else ("HOLD" if returning else "FAILED")
                        fields = {"hold_code": "RETURN_FAILED"} if returning and not sent else {}
                        if not store.claim_order(order["id"], ("UNCERTAIN",), status, note="管理サイトで明細を照合", **fields):
                            raise ValueError("注文の状態が変わりました。")
                        reply.messages.append("明細の照合結果を記録しました。")
                elif action == "settings":
                    store.update_config(enabled=form.get("enabled") == "yes", auto_send=form.get("auto_send") == "yes")
                    target = "/connections"
                    reply.messages.append("設定を保存しました。")
                else:
                    raise web.HTTPNotFound()
            except ValueError as exc:
                current["message"] = str(exc)
            else:
                current["message"] = " / ".join(reply.messages)
            raise web.HTTPSeeOther(target)

    async def health(request):
        return web.Response(text="ok")

    async def style(request):
        return web.FileResponse(ROOT / "style.css")

    app.router.add_get("/health", health)
    app.router.add_get("/style.css", style)
    app.router.add_get("/login", login)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_get("/", dashboard)
    app.router.add_get("/{page:history|connections}", dashboard)
    app.router.add_post("/do/{action}", submit)
    return app


def hash_password(password, salt):
    return hashlib.scrypt(password, salt=salt, n=16384, r=8, p=1).hex()


async def maintenance(app):
    async def periodic(function, interval):
        while True:
            try:
                await function()
            except Exception as exc:
                logging.error("定期処理 %s: %s", function.__name__, type(exc).__name__)
            await asyncio.sleep(interval)
    lock = exchange.acquire_single_instance_lock()
    try:
        store.load_orders()
        await exchange.recover_stuck_orders()
        await exchange.recover_receiving_holds()
        jobs = [asyncio.create_task(periodic(fn, seconds)) for fn, seconds in (
            (exchange.watch_holds.coro, 30), (exchange._reidentify_once, 120),
            (exchange.keep_session.coro, 1500), (exchange.keep_kyash.coro, 21600))]
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
    finally:
        lock.close()


if __name__ == "__main__":
    app = make_app("/etc/paykyash/web-password.hash")
    app.cleanup_ctx.append(maintenance)
    web.run_app(app, host=os.environ["EXCHANGE_BIND"], port=8091, access_log=None)
