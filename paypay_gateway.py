"""Beta-only PayPay P2P link gateway.

This uses the owner-provided, unofficial PayPay web protocol. It is deliberately
fail-closed: credentials are encrypted with a stable Fernet key, and an
uncertain accept result is never retried automatically.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from cryptography.fernet import Fernet

import github_runtime_store

TOKEN_ENDPOINT = "https://www.paypay.ne.jp/app/v1/oauth/token"
LINK_CHECK_ENDPOINT = "https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo"
LINK_ACCEPT_ENDPOINT = "https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink"
USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
LINK_RE = re.compile(r"https://pay\.paypay\.ne\.jp/([A-Za-z0-9]{6,128})(?![A-Za-z0-9])", re.I)
CODE_RE = re.compile(r"[A-Za-z0-9]{6,128}\Z")
UUID_RE = re.compile(r"^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$")
OTP_RE = re.compile(r"^[0-9]{4}$")
SESSION_MAX_SECONDS = 7200
HOLD_CODES = {"42007013", "42007014", "42007015", "42007016"}
# getP2PLinkInfo の orderStatus。受け取り済みは "SUCCESS"（受け取りPOSTの応答は "COMPLETED"）。
RECEIVED_STATUSES = {"SUCCESS", "COMPLETED"}
CANCELLED_STATUSES = {"CANCELED", "CANCELLED", "REJECTED", "REFUNDED", "FAILED"}
_ROOT = Path(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay")).resolve()
_ACCOUNT_FILE = _ROOT / "account.enc"
_LEDGER_FILE = Path(os.environ.get("PAYPAY_LEDGER_PATH", str(_ROOT / "payments.sqlite3")))
_LOCK = threading.Lock()
_pending_otp: dict = {}
_logger = logging.getLogger(__name__)

class PayPayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

def _cipher():
    key = os.environ.get("PAYPAY_ENCRYPTION_KEY", "").strip()
    if not key:
        raise PayPayError("missing_key", "PAYPAY_ENCRYPTION_KEY が未設定です。")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise PayPayError("invalid_key", "PAYPAY_ENCRYPTION_KEY の形式が正しくありません。") from None

def _save_account(account: dict):
    raw = _cipher().encrypt(json.dumps(account).encode())
    if github_runtime_store.enabled():
        try:
            github_runtime_store.write_blob("paypay/account.enc", raw)
        except github_runtime_store.RuntimeStoreError as exc:
            raise PayPayError("runtime_store", str(exc)) from exc
        return
    _ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".account-", dir=_ROOT)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, _ACCOUNT_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def _load_account():
    try:
        raw = github_runtime_store.read_blob("paypay/account.enc", _ACCOUNT_FILE)
    except github_runtime_store.RuntimeStoreError as exc:
        raise PayPayError("runtime_store", str(exc)) from exc
    if not raw:
        return None
    try:
        data = json.loads(_cipher().decrypt(raw))
        required = ("phone", "password", "uuid", "access_token", "session_cookie", "expires_at")
        if not isinstance(data, dict) or not all(data.get(k) for k in required):
            raise ValueError()
        if not UUID_RE.fullmatch(data["uuid"]):
            raise ValueError()
        return data
    except PayPayError:
        raise
    except Exception:
        raise PayPayError("account_unreadable", "保存済みPayPay認証情報を読み取れません。") from None

def status():
    account = _load_account()
    valid = bool(account and float(account.get("expires_at", 0)) > time.time() + 60
                 and account.get("session_cookie"))
    return {"connected": bool(account), "ready": valid,
            "expires_at": int(account.get("expires_at", 0)) if account else 0}

def _headers(referer: str, json_body=True):
    result = {"Accept": "application/json, text/plain, */*",
              "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
              "Origin": "https://www.paypay.ne.jp", "Referer": referer,
              "User-Agent": USER_AGENT}
    if json_body:
        result["Content-Type"] = "application/json"
    return result

async def _oauth(payload: dict, referer: str, cookies: dict | None = None):
    from yarl import URL
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if cookies:
                session.cookie_jar.update_cookies(cookies, response_url=URL("https://www.paypay.ne.jp/"))
            async with session.post(TOKEN_ENDPOINT, json=payload,
                                    headers=_headers(referer)) as response:
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    _logger.warning("PayPay OAuth response was not JSON (http_status=%s content_type=%s)",
                                    response.status, (response.headers.get("Content-Type", "") or "")[:80])
                    raise PayPayError("oauth_response", "PayPayの認証応答を読み取れません。") from None
                if not isinstance(body, dict):
                    _logger.warning("PayPay OAuth response had unexpected JSON type (http_status=%s body_type=%s)",
                                    response.status, type(body).__name__)
                    raise PayPayError("oauth_response", "PayPayの認証応答が不正です。")
                jar = session.cookie_jar.filter_cookies(URL("https://www.paypay.ne.jp/"))
                cookie_values = {name: morsel.value for name, morsel in jar.items()}
                session_cookie = cookie_values.get("token", "")
                if response.status in range(200, 300) and isinstance(body.get("access_token"), str) and len(body["access_token"]) >= 10:
                    raw_expiry = body.get("expires_in")
                    expires_in = min(int(raw_expiry), SESSION_MAX_SECONDS) if type(raw_expiry) in (int, float) and raw_expiry > 0 else SESSION_MAX_SECONDS
                    return {"kind": "token", "access_token": body["access_token"],
                            "expires_in": expires_in, "session_cookie": session_cookie,
                            "cookies": cookie_values}
                otp_id, prefix = body.get("otp_reference_id"), body.get("otp_prefix")
                if isinstance(otp_id, str) and otp_id and isinstance(prefix, str) and re.fullmatch(r"[A-Za-z]{2,8}", prefix):
                    return {"kind": "otp", "otp_reference_id": otp_id, "otp_prefix": prefix,
                            "cookies": cookie_values}
                # Record only protocol metadata. Never log response values, credentials,
                # tokens, cookies, OTPs, phone numbers, or the raw provider body.
                provider_code = None
                response_shape = []
                code_fields = {"resultCode", "result_code", "errorCode", "error_code", "code"}
                # Inspect only a shallow, bounded structure. Log field paths and
                # numeric provider codes, never arbitrary values or raw bodies.
                stack = [(body, "", 0)]
                while stack and len(response_shape) < 40:
                    node, prefix, depth = stack.pop()
                    if isinstance(node, dict):
                        for key, value in list(node.items())[:20]:
                            if not isinstance(key, str):
                                continue
                            path = f"{prefix}.{key}" if prefix else key
                            if len(path) <= 120:
                                response_shape.append(f"{path}:{type(value).__name__}")
                            if provider_code is None and key in code_fields and isinstance(value, (str, int)):
                                if re.fullmatch(r"(?:[A-Z][0-9]{4}|[0-9]{4,12})", str(value)):
                                    provider_code = str(value)
                            if depth < 3 and isinstance(value, (dict, list)):
                                stack.append((value, path, depth + 1))
                    elif isinstance(node, list):
                        for index, value in enumerate(node[:10]):
                            if depth < 3 and isinstance(value, (dict, list)):
                                stack.append((value, f"{prefix}[{index}]", depth + 1))
                _logger.warning("PayPay OAuth rejected (http_status=%s provider_code=%s response_shape=%s)",
                                response.status, provider_code or "unavailable",
                                ",".join(response_shape[:40]))
                raise PayPayError("oauth_rejected", "PayPayログインに失敗しました。認証情報が正しい場合もあります。管理者ログのPayPay OAuth診断情報を確認してください。")
    except PayPayError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise PayPayError("oauth_network", "PayPay認証中に通信エラーが発生しました。") from None

async def begin_login(phone: str, password: str):
    _cipher()
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone or "") or not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise PayPayError("invalid_credentials", "電話番号またはパスワードの形式を確認してください。")
    existing = _load_account()
    uuid = existing["uuid"] if existing and existing.get("phone") == phone else str(uuid4())
    result = await _oauth({"scope":"SIGN_IN","client_uuid":uuid,"grant_type":"password",
                           "username":phone,"password":password,"add_otp_prefix":True,"language":"ja"},
                          "https://www.paypay.ne.jp/app/account/sign-in")
    if result["kind"] == "otp":
        _pending_otp.clear()
        _pending_otp.update(phone=phone, password=password, uuid=uuid,
                            otp_reference_id=result["otp_reference_id"], otp_prefix=result["otp_prefix"],
                            cookies=result.get("cookies", {}))
        return {"otp_required": True, "prefix": result["otp_prefix"]}
    if not result["session_cookie"]:
        raise PayPayError("cookie_missing", "PayPayログインは応答しましたが、受取セッションを確認できません。")
    _save_account({"phone":phone,"password":password,"uuid":uuid,
                   "access_token":result["access_token"],"session_cookie":result["session_cookie"],
                   "expires_at":time.time()+result["expires_in"]})
    _pending_otp.clear()
    _relogin_block["until"] = 0.0
    return {"otp_required": False}

async def finish_login(otp: str):
    _cipher()
    p = dict(_pending_otp)
    if not p:
        raise PayPayError("otp_expired", "SMS認証の有効期限が切れました。最初からログインしてください。")
    if not OTP_RE.fullmatch(otp or ""):
        raise PayPayError("otp_format", "SMS認証コードは4桁で入力してください。")
    result = await _oauth({"scope":"SIGN_IN","grant_type":"otp","client_uuid":p["uuid"],
                           "otp":otp,"otp_prefix":p["otp_prefix"],
                           "otp_reference_id":p["otp_reference_id"],
                           "username_type":"MOBILE","language":"ja"},
                          "https://www.paypay.ne.jp/app/account/sign-in", cookies=p.get("cookies"))
    if result["kind"] != "token" or not result["session_cookie"]:
        raise PayPayError("otp_failed", "SMS認証に失敗しました。")
    _save_account({"phone":p["phone"],"password":p["password"],"uuid":p["uuid"],
                   "access_token":result["access_token"],"session_cookie":result["session_cookie"],
                   "expires_at":time.time()+result["expires_in"]})
    _pending_otp.clear()
    _relogin_block["until"] = 0.0
    return {"connected": True}

def _normalize_link(raw: str):
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    raw = raw.strip()
    if CODE_RE.fullmatch(raw):
        return raw
    urls = LINK_RE.findall(raw)
    if not urls or len(set(urls)) != 1:
        return None
    for url in re.findall(r"https?://[^\s<>\[\]()\"']+", raw, re.I):
        try:
            u = urlsplit(url.rstrip(".,;。"))
            if u.scheme != "https" or u.hostname != "pay.paypay.ne.jp" or not re.fullmatch("/" + re.escape(urls[0]) + "/?", u.path):
                return None
            if u.username or u.password or u.port or u.fragment or u.query:
                return None
        except ValueError:
            return None
    return urls[0]

def _link_problem(raw):
    """リンクが読み取れない理由を細かく返す。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return PayPayError("empty_link", "送金リンクが入力されていません。")
    if len(text) > 2048:
        return PayPayError("link_too_long", "入力が長すぎます。送金リンクだけを貼り付けてください。")
    urls = LINK_RE.findall(text)
    if not urls:
        return PayPayError("not_paypay_link", "PayPayの送金リンク（https://pay.paypay.ne.jp/…）ではありません。")
    if len(set(urls)) > 1:
        return PayPayError("multiple_links", "リンクが複数含まれています。1件だけ貼り付けてください。")
    return PayPayError("bad_link", "送金リンクの形式が正しくありません。リンクの前後に余計な文字がないか確認してください。")

def _parse_time(value):
    if not isinstance(value, str): return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except ValueError: return None

def _verify_link(body, amount):
    header = body.get("header") if isinstance(body, dict) else None
    if not isinstance(header, dict) or header.get("resultCode") != "S0000":
        raise PayPayError("invalid_link", "PayPayリンクを確認できませんでした。")
    p = body.get("payload")
    status = p.get("orderStatus") if isinstance(p, dict) else None
    if status in RECEIVED_STATUSES:
        raise PayPayError("already_received", "このリンクはすでに受け取り済みです。新しい送金リンクを作ってください。")
    if status in CANCELLED_STATUSES:
        raise PayPayError("link_cancelled", "このリンクは取り消し・辞退されています。新しい送金リンクを作ってください。")
    if status == "EXPIRED":
        raise PayPayError("expired", "このリンクは有効期限が切れています。新しい送金リンクを作ってください。")
    if not isinstance(p, dict) or status != "PENDING":
        raise PayPayError("not_pending", "このPayPayリンクは未受取状態ではありません。")
    info, msg = p.get("pendingP2PInfo"), p.get("message")
    if not isinstance(info, dict) or not isinstance(msg, dict) or type(info.get("amount")) is not int:
        raise PayPayError("link_incomplete", "送金リンクの金額を確認できません。")
    if info["amount"] != amount:
        raise PayPayError("amount_mismatch", f"金額が違います（リンク {info['amount']:,}円／必要 {amount:,}円）。{amount:,}円ちょうどの新しいリンクを作ってください。")
    if info.get("isSetPasscode") is not False:
        raise PayPayError("link_restricted", "パスコード付きのリンクは使えません。パスコードなしで作り直してください。")
    if info.get("isLinkBlocked") is not False:
        # PayPay側の保留中は、未受取のまま「ブロック中」になる。送金者がアプリで解除すると受け取れる。
        raise PayPayError("link_blocked", "このリンクはPayPay側で保留中です。PayPayアプリで保留を解除してから、もう一度お試しください（再送金は不要です）。")
    expiry = _parse_time(info.get("expiredAt"))
    if not expiry or expiry <= datetime.now(timezone.utc):
        raise PayPayError("expired", "リンクの有効期限が切れているか、確認できません。新しいリンクを作ってください。")
    sender, receiver = p.get("sender"), p.get("receiver")
    if not isinstance(sender, dict) or not isinstance(receiver, dict) or not sender.get("externalId") or not receiver.get("externalId") or sender["externalId"] == receiver["externalId"]:
        raise PayPayError("identity", "送金元・受取先を確認できません。自分宛てのリンクは使えません。")
    data = msg.get("data")
    if (not isinstance(data, dict) or not isinstance(info.get("orderId"), str) or not info["orderId"]
            or data.get("orderId") != info.get("orderId") or data.get("amount") != amount or data.get("status") != "PENDING"):
        raise PayPayError("order_mismatch", "送金リンクの注文情報が一致しません。")
    if not all(isinstance(msg.get(k), str) and msg[k] for k in ("messageId", "chatRoomId")) or not _parse_time(data.get("transactionAt")):
        raise PayPayError("link_incomplete", "送金リンクの取引情報が不足しています。")
    return {"order_id":info["orderId"],"message_id":msg["messageId"],"chat_id":msg["chatRoomId"],
            "request_at":data["transactionAt"],"receiver":str(receiver["externalId"]),
            "sender_id":str(sender["externalId"])[:80],
            "sender_name":(sender.get("displayName") if isinstance(sender.get("displayName"), str) else "")[:40]}

def _provider_hold_code(body):
    if not isinstance(body, dict):
        return None
    candidates = []
    def collect(obj):
        if not isinstance(obj, dict):
            return
        for field in ("resultCode", "result_code", "errorCode", "error_code", "code"):
            value = obj.get(field)
            if type(value) in (str, int):
                candidates.append(str(value))
    collect(body)
    for key in ("header", "error", "result_info", "resultInfo", "payload"):
        collect(body.get(key))
    errors = body.get("errors")
    if isinstance(errors, list):
        for item in errors[:10]:
            collect(item)
    return next((code for code in candidates if code in HOLD_CODES), None)


@contextmanager
def _ledger():
    try:
        with github_runtime_store.sqlite_database("paypay-ledger.sqlite3", _LEDGER_FILE) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS links (
                digest TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS history (
                order_id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, user_id TEXT,
                amount INTEGER, state TEXT, summary TEXT, diag TEXT
            )""")
            for column in ("receiver TEXT", "last_status TEXT", "amount INTEGER", "used INTEGER", "sender_id TEXT", "sender_name TEXT", "accept_status TEXT"):
                try:
                    db.execute("ALTER TABLE links ADD COLUMN " + column)
                except sqlite3.OperationalError:
                    continue  # 追加済み
                if column.startswith("used"):
                    # この列ができる前に支払い済みになったリンクは「使用済み」とみなす（使い回し防止）
                    db.execute("UPDATE links SET used=1 WHERE state='paid'")
            if not github_runtime_store.enabled():
                os.chmod(_LEDGER_FILE, 0o600)
            yield db
    except github_runtime_store.RuntimeStoreError as exc:
        raise PayPayError("runtime_store", str(exc)) from exc

# 自動の再ログインを休む時間（秒）。保留の見張り（90秒ごと）やお客さんの支払いのたびにパスワードで
# ログインし直すと、SMS が何通も飛び、管理者が入力中の認証コードも無効になる。失敗したらしばらく試さない。
RELOGIN_WAIT = {"otp_required": 600, "oauth_rejected": 600, "cookie_missing": 300}
RELOGIN_WAIT_DEFAULT = 60        # 通信エラーなど
_relogin_block: dict = {"until": 0.0, "code": "", "message": ""}


def _block_relogin(exc: PayPayError) -> None:
    _relogin_block.update(until=time.time() + RELOGIN_WAIT.get(exc.code, RELOGIN_WAIT_DEFAULT),
                          code=exc.code, message=exc.message)


async def _fresh_account(force: bool = False, manual: bool = False):
    """保存済みアカウントを読み、期限切れなら再ログインして返す。呼び出し側で _LOCK を保持すること。
    force=True: PayPayがセッションを拒否した（401/403）ときに、期限内でもログインし直す。
    manual=True: 管理者が「接続を更新」を押したとき。休み時間（RELOGIN_WAIT）を無視して今すぐ試す。"""
    account = _load_account()
    if not account:
        raise PayPayError("login_required", "現在、決済を受け付けられません（受取側のPayPay未接続）。管理者にお知らせください。")
    if force or float(account.get("expires_at",0)) <= time.time()+60 or not account.get("session_cookie"):
        if not manual and time.time() < _relogin_block["until"]:
            raise PayPayError(_relogin_block["code"], _relogin_block["message"])
        try:
            renewed = await _oauth({"scope":"SIGN_IN","client_uuid":account["uuid"],"grant_type":"password",
                                    "username":account["phone"],"password":account["password"],
                                    "add_otp_prefix":True,"language":"ja"},
                                   "https://www.paypay.ne.jp/app/account/sign-in")
        except PayPayError as exc:
            _block_relogin(exc)
            raise
        if renewed["kind"] == "otp":
            _pending_otp.clear()
            _pending_otp.update(phone=account["phone"],password=account["password"],uuid=account["uuid"],
                                otp_reference_id=renewed["otp_reference_id"],otp_prefix=renewed["otp_prefix"],
                                cookies=renewed.get("cookies", {}))
            exc = PayPayError("otp_required", "現在、決済を受け付けられません（受取側のPayPayが再認証待ち）。管理者にお知らせください。")
            _block_relogin(exc)
            raise exc
        if not renewed.get("session_cookie"):
            exc = PayPayError("cookie_missing", "PayPayの再認証結果を確認できません。")
            _block_relogin(exc)
            raise exc
        _relogin_block["until"] = 0.0
        account.update(access_token=renewed["access_token"],session_cookie=renewed["session_cookie"],
                       expires_at=time.time()+renewed["expires_in"])
        _save_account(account)
    return account

async def _accept(session, account, code, verified, digest):
    """受け取りPOST。成功で digest を返す。保留/不明は台帳を更新して PayPayError。自動で再試行しない。"""
    payload={"requestAt":verified["request_at"],"orderId":verified["order_id"],"requestId":str(uuid4()),
             "senderChannelUrl":verified["chat_id"],"senderMessageId":verified["message_id"],
             "verificationCode":code,"client_uuid":account["uuid"],
             "iosMinimumVersion":"3.45.0","androidMinimumVersion":"3.45.0"}
    try:
        async with session.post(LINK_ACCEPT_ENDPOINT,json=payload) as r:
            status_code=r.status
            try: reply=await r.json(content_type=None)
            except Exception: reply=None
    except (aiohttp.ClientError,asyncio.TimeoutError):
        with _ledger() as db: db.execute("UPDATE links SET state='review' WHERE digest=?",(digest,))
        raise PayPayError("accept_uncertain","受取結果が不明です。同じリンクを再送せず、管理者へお問い合わせください。") from None
    try:  # 診断用: 受け取りPOSTの応答コードだけを記録
        rc = reply.get("header", {}).get("resultCode") if isinstance(reply, dict) and isinstance(reply.get("header"), dict) else None
        rc = rc if isinstance(rc, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", rc) else "-"
        with _ledger() as db: db.execute("UPDATE links SET last_status=?, accept_status=? WHERE digest=?",(f"accept:{status_code}/{rc}/{_provider_hold_code(reply) or '-'}"[:80],f"{status_code}/{rc}/{_provider_hold_code(reply) or '-'}"[:80],digest))
    except Exception:
        pass
    ok=(status_code==200 and isinstance(reply,dict) and isinstance(reply.get("header"),dict)
        and reply["header"].get("resultCode")=="S0000" and isinstance(reply.get("payload"),dict)
        and reply["payload"].get("orderStatus")=="COMPLETED" and reply.get("error") is None)
    hold_code = _provider_hold_code(reply)
    state = "paid" if ok else ("pending" if hold_code else "review")
    try:
        with _ledger() as db: db.execute("UPDATE links SET state=? WHERE digest=?",(state,digest))
    except PayPayError:
        # 受け取りPOSTは送ったあと。台帳に書けなかっただけで「未払い」に戻すと、お金が動いたのに
        # 注文が進まなくなる。結果不明（保留）として残し、同じリンクの再確認で拾えるようにする。
        raise PayPayError("accept_uncertain","受取結果を記録できませんでした。同じリンクを再送せず、少し待ってから「今すぐ確認する」を押してください。") from None
    if ok:
        return digest
    raise PayPayError("temporary_hold" if state=="pending" else "accept_unconfirmed",
                      "PayPay側で一時保留です。状態を確認するまで再送金しないでください。" if state=="pending"
                      else "受取結果を確定できません。再購入せず管理者へお問い合わせください。")

async def _receive_once(code: str, amount: int, purchase_id: str, force_login: bool):
    with _LOCK:
        account = await _fresh_account(force=force_login)
        referer=f"https://www.paypay.ne.jp/app/p2p/{code}?pid=SMS&link_key={code}&af_dp=paypay%3A%2F%2Fpending%2Fdetail%3Fsource%3Dsns_link"
        headers=_headers(referer)
        timeout=aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout,headers=headers,cookies={"token":account["session_cookie"]}) as session:
            try:
                async with session.get(LINK_CHECK_ENDPOINT,params={"verificationCode":code,"client_uuid":account["uuid"]}) as r:
                    if r.status == 404:
                        raise PayPayError("link_not_found", "このPayPayリンクは見つかりません。リンクが間違っているか、取り消されています。")
                    if r.status in (401, 403):
                        raise PayPayError("session_rejected", "受取側のPayPay接続が切れています。管理者に再接続を依頼してください（送金はしないでください）。")
                    if r.status == 429:
                        raise PayPayError("rate_limited", "PayPayが混み合っています。しばらくしてからもう一度お試しください。")
                    if r.status >= 500:
                        raise PayPayError("paypay_unavailable", "PayPay側で一時的なエラーが起きています。しばらくしてからお試しください。")
                    if r.status != 200:
                        raise PayPayError("link_check", f"PayPayリンクを確認できませんでした（応答 {r.status}）。")
                    body=await r.json(content_type=None)
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError):
                raise PayPayError("link_network", "PayPayリンク確認で通信エラーが発生しました。") from None
            verified=_verify_link(body,amount)
            digest=hashlib.sha256(code.encode()).hexdigest()
            with _ledger() as db:
                try: db.execute("INSERT INTO links(digest,order_id,state,created,receiver,amount,sender_id,sender_name) VALUES(?,?,?,?,?,?,?,?)",(digest,purchase_id,"attempted",time.time(),verified["receiver"],amount,verified["sender_id"],verified["sender_name"]))
                except sqlite3.IntegrityError:
                    raise PayPayError("duplicate","このリンクまたは注文はすでに使用済みです。新しい送金リンクを作ってください。") from None
            return await _accept(session, account, code, verified, digest)


async def receive_payment(raw_link: str, amount: int, purchase_id: str):
    code = _normalize_link(raw_link)
    if not code: raise _link_problem(raw_link)
    # このリンクをすでに受け付けたことがあるか、先に台帳で確認する。
    # （保留が解除されて入金済みになったリンクを貼り直したとき、「受け取り済み」と弾く前に自分の入金だと分かるように）
    with _ledger() as db:
        known = db.execute("SELECT order_id FROM links WHERE digest=?", (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
    if known:
        if known["order_id"] == purchase_id:
            return await confirm_received(code, purchase_id, amount)
        raise PayPayError("duplicate", "このリンクは以前の注文で使われています。")
    try:
        return await _receive_once(code, amount, purchase_id, False)
    except PayPayError as exc:
        if exc.code != "session_rejected":
            raise
    # リンク確認（GET）でセッションを拒否された → ログインし直してもう一度だけ（受け取りPOSTの前なので安全）
    return await _receive_once(code, amount, purchase_id, True)


async def confirm_received(code: str, purchase_id: str, amount: int | None = None):
    try:
        return await _confirm_once(code, purchase_id, amount, False)
    except PayPayError as exc:
        if exc.code != "session_rejected":
            raise
    return await _confirm_once(code, purchase_id, amount, True)


async def _confirm_once(code: str, purchase_id: str, amount: int | None, force_login: bool):
    """保留・結果不明になった注文の再確認。新しいリンクは受け付けず、その注文で最初に送られたリンクだけを扱う。
    - COMPLETED（受け取り済み）→ 支払い済み
    - PENDING（送金者が保留を解除した等で、まだ受け取っていない）→ 金額等を再検証してもう一度受け取る
    """
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        raise PayPayError("not_found", "確認できる送金リンクがありません。")
    digest = hashlib.sha256(code.encode()).hexdigest()
    with _ledger() as db:
        row = db.execute("SELECT order_id, state, receiver FROM links WHERE digest=?", (digest,)).fetchone()
    if not row or row["order_id"] != purchase_id:
        raise PayPayError("not_found", "この注文の送金記録が見つかりません。")
    if row["state"] == "paid":
        return digest
    if row["state"] not in ("pending", "review", "attempted"):
        raise PayPayError("not_found", "この送金は確認できない状態です。")
    with _LOCK:
        account = await _fresh_account(force=force_login)
        referer = f"https://www.paypay.ne.jp/app/p2p/{code}?pid=SMS&link_key={code}&af_dp=paypay%3A%2F%2Fpending%2Fdetail%3Fsource%3Dsns_link"
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers(referer), cookies={"token": account["session_cookie"]}) as session:
            try:
                async with session.get(LINK_CHECK_ENDPOINT, params={"verificationCode": code, "client_uuid": account["uuid"]}) as r:
                    if r.status in (401, 403):
                        raise PayPayError("session_rejected", "受取側のPayPay接続が切れています。")
                    if r.status == 429:
                        raise PayPayError("rate_limited", "PayPayが混み合っています。しばらくしてからもう一度お試しください。")
                    if r.status >= 500:
                        raise PayPayError("paypay_unavailable", "PayPay側で一時的なエラーが起きています。しばらくしてからお試しください。")
                    if r.status != 200:
                        raise PayPayError("link_check", f"PayPayリンクを確認できませんでした（応答 {r.status}）。")
                    body = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                raise PayPayError("link_network", "PayPayリンク確認で通信エラーが発生しました。") from None
            header = body.get("header") if isinstance(body, dict) else None
            payload = body.get("payload") if isinstance(body, dict) else None
            status = payload.get("orderStatus") if isinstance(payload, dict) else None
            status = status if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,40}", status) else None
            result_code = header.get("resultCode") if isinstance(header, dict) else None
            with _ledger() as db:  # 診断用に、PayPayが返した状態名だけを記録（個人情報は含まない）
                db.execute("UPDATE links SET last_status=? WHERE digest=?", (f"{result_code}/{status}"[:80], digest))
            if result_code != "S0000":
                raise PayPayError("still_pending", "PayPayリンクの状態を確認できませんでした。少し待ってからもう一度お試しください。")
            if status in RECEIVED_STATUSES:
                # 受け取り済み。受け取ったのがこのアカウントか確認する（他人が受け取ったリンクを支払い済みにしない）。
                receiver = payload.get("receiver") if isinstance(payload.get("receiver"), dict) else {}
                got = receiver.get("externalId")
                expected = row["receiver"]
                if got and expected and str(got) != str(expected):
                    with _ledger() as db:
                        db.execute("UPDATE links SET state='review' WHERE digest=?", (digest,))
                    raise PayPayError("receiver_mismatch", "このリンクは別のアカウントで受け取られている可能性があります。管理者に連絡してください。")
                with _ledger() as db:
                    db.execute("UPDATE links SET state='paid' WHERE digest=?", (digest,))
                return digest
            if status == "PENDING":
                info = payload.get("pendingP2PInfo") if isinstance(payload.get("pendingP2PInfo"), dict) else {}
                if info.get("isLinkBlocked") is True:
                    # 保留中（未受取＋ブロック中）。解除されるまで待つ。解除後は自動で入金済み（SUCCESS）になるか、もう一度受け取れる。
                    raise PayPayError("temporary_hold", "PayPay側でまだ保留中です。PayPayアプリで保留を解除すると、自動で確認して実行します。")
            if status == "PENDING" and amount is not None:
                verified = _verify_link(body, amount)  # 金額・期限・送金者などを最初と同じ基準で再確認
                return await _accept(session, account, code, verified, digest)
            if status in CANCELLED_STATUSES or status == "EXPIRED":
                raise PayPayError("hold_failed", "このリンクは受け取れない状態になっています（取り消し・期限切れなど）。管理者に連絡してください。")
    # PENDING（金額不明）や未知の状態は、注文を無効にせず保留のままにする
    raise PayPayError("still_pending", "まだ受け取りが完了していません。少し待ってからもう一度お試しください。")


def mark_used(code: str):
    """このリンクの支払いで実行を開始した（以後、別の注文では使えない）。"""
    if isinstance(code, str) and CODE_RE.fullmatch(code):
        with _ledger() as db:
            db.execute("UPDATE links SET used=1 WHERE digest=?", (hashlib.sha256(code.encode()).hexdigest(),))


def adopt_link(code: str, new_purchase_id: str, amount: int):
    """以前の注文（保留・停止のまま実行されなかったもの）で送られたリンクを、同じ金額の新しい注文に付け替える。
    実行に使われたリンク、金額の違うリンクは付け替えない。呼び出し側で古い注文を無効にしておくこと。"""
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        raise PayPayError("not_found", "確認できる送金リンクがありません。")
    digest = hashlib.sha256(code.encode()).hexdigest()
    with _ledger() as db:
        row = db.execute("SELECT state, amount, used FROM links WHERE digest=?", (digest,)).fetchone()
        if not row:
            raise PayPayError("not_found", "この送金リンクの記録が見つかりません。")
        if row["used"]:
            raise PayPayError("duplicate", "このリンクはすでに実行に使われています。新しい送金リンクを作ってください。")
        if row["amount"] is None:
            raise PayPayError("needs_admin", "このリンクは以前の注文で使われています。お支払い済みの場合は、注文番号を管理者に伝えてください。")
        if int(row["amount"]) != int(amount):
            raise PayPayError("amount_mismatch", f"金額が違います（リンク {int(row['amount']):,}円／必要 {int(amount):,}円）。このリンクは {int(row['amount']):,}円 の注文で使えます。")
        try:
            db.execute("UPDATE links SET order_id=? WHERE digest=?", (new_purchase_id, digest))
        except sqlite3.IntegrityError:
            raise PayPayError("duplicate", "この注文には別の送金リンクが記録されています。") from None


def history_upsert(rows):
    """取引履歴（注文ごとに1行）。引継ぎコードなどの秘密は入れない。"""
    if not rows:
        return
    now = time.time()
    with _ledger() as db:
        for r in rows:
            db.execute("""INSERT INTO history(order_id,created,updated,user_id,amount,state,summary,diag) VALUES(?,?,?,?,?,?,?,?)
                          ON CONFLICT(order_id) DO UPDATE SET updated=excluded.updated, user_id=excluded.user_id, amount=excluded.amount,
                          state=excluded.state, summary=excluded.summary, diag=excluded.diag
                          WHERE history.state IS NOT excluded.state OR history.diag IS NOT excluded.diag OR history.user_id IS NOT excluded.user_id""",
                       (r["order_id"], r["created"], now, r["user_id"], r["amount"], r["state"], r["summary"], r["diag"]))


def history_list(limit: int = 200, user_id: str = ""):
    with _ledger() as db:
        sql = """SELECT h.order_id, h.created, h.updated, h.user_id, h.amount, h.state, h.summary, h.diag,
                        l.sender_id, l.sender_name, l.state AS pay_state, l.last_status
                 FROM history h LEFT JOIN links l ON l.order_id = h.order_id"""
        args = []
        if user_id:
            sql += " WHERE h.user_id = ? OR l.sender_id = ?"
            args += [user_id, user_id]
        sql += " ORDER BY h.created DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in db.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- 実績（お客さんの評価と感想）
def _review_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE NOT NULL,
                  created REAL NOT NULL, shop TEXT NOT NULL, stars INTEGER NOT NULL, body TEXT NOT NULL,
                  hidden INTEGER NOT NULL DEFAULT 0)""")


def review_add(order_id: str, shop: str, stars: int, body: str) -> bool:
    """実績を1件保存する。1つの注文につき1件だけ（すでにあれば False）。"""
    with _ledger() as db:
        _review_table(db)
        try:
            db.execute("INSERT INTO reviews(order_id, created, shop, stars, body) VALUES(?,?,?,?,?)",
                       (order_id, time.time(), shop, int(stars), body))
            return True
        except sqlite3.IntegrityError:
            return False


def review_list(limit: int = 30, before: int | None = None, include_hidden: bool = False):
    """新しい順。before は「この id より前」（続きを読むとき）。注文番号は返さない。"""
    sql, args = "SELECT id, created, shop, stars, body, hidden FROM reviews WHERE 1=1", []
    if not include_hidden:
        sql += " AND hidden=0"
    if before:
        sql += " AND id < ?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _ledger() as db:
        _review_table(db)
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def review_stats() -> dict:
    """公開中の実績の件数・平均・星ごとの件数。"""
    with _ledger() as db:
        _review_table(db)
        rows = db.execute("SELECT stars, COUNT(*) AS n FROM reviews WHERE hidden=0 GROUP BY stars").fetchall()
    dist = {i: 0 for i in range(1, 6)}
    for r in rows:
        if r["stars"] in dist:
            dist[r["stars"]] = r["n"]
    count = sum(dist.values())
    return {"count": count, "average": round(sum(k * v for k, v in dist.items()) / count, 1) if count else 0.0, "dist": dist}


def review_set_hidden(review_id: int, hidden: bool) -> bool:
    with _ledger() as db:
        _review_table(db)
        return db.execute("UPDATE reviews SET hidden=? WHERE id=?", (1 if hidden else 0, int(review_id))).rowcount == 1


# ---------------------------------------------------------------- クーポン（200円以上で10%引き・1回きり）
_COUPON_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 見間違えやすい 0/O/1/I を除く


def _coupon_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS coupons (code TEXT PRIMARY KEY, created REAL, expires REAL, state TEXT,
                  issued_order TEXT, used_order TEXT, updated REAL)""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(coupons)").fetchall()}
    if "kind" not in cols:   # 種類：pct10＝10%引き（自動発行）／free＝1回だけ無料（管理者が発行）
        db.execute("ALTER TABLE coupons ADD COLUMN kind TEXT NOT NULL DEFAULT 'pct10'")
    if "note" not in cols:
        db.execute("ALTER TABLE coupons ADD COLUMN note TEXT")


def _new_coupon_code(prefix: str) -> str:
    import secrets as _s
    return prefix + "-" + "".join(_s.choice(_COUPON_CHARS) for _ in range(4)) + "-" + "".join(_s.choice(_COUPON_CHARS) for _ in range(4))


def coupon_issue(order_id: str, days: int = 30) -> str:
    now = time.time()
    with _ledger() as db:
        _coupon_table(db)
        row = db.execute("SELECT code FROM coupons WHERE issued_order=?", (order_id,)).fetchone()
        if row:
            return row["code"]
        for _ in range(10):
            code = _new_coupon_code("NK")
            try:
                db.execute("INSERT INTO coupons(code, created, expires, state, issued_order, used_order, updated, kind) "
                           "VALUES(?,?,?,?,?,?,?,'pct10')", (code, now, now + days * 86400, "active", order_id, None, now))
                return code
            except sqlite3.IntegrityError:
                continue
    raise PayPayError("coupon_issue", "クーポンを発行できませんでした。")


FREE_COUPON_PREFIX = "FR"


def coupon_issue_free(days: int = 30, note: str = "") -> str:
    """管理者が発行する「1回だけ無料」のクーポン（FR-XXXX-XXXX）。金額に関係なく、1回の注文が0円になる。"""
    days = max(1, min(int(days), 365))
    note = str(note or "").strip()[:60] or None
    now = time.time()
    with _ledger() as db:
        _coupon_table(db)
        for _ in range(10):
            code = _new_coupon_code(FREE_COUPON_PREFIX)
            try:
                db.execute("INSERT INTO coupons(code, created, expires, state, issued_order, used_order, updated, kind, note) "
                           "VALUES(?,?,?,?,?,?,?,'free',?)", (code, now, now + days * 86400, "active", "admin", None, now, note))
                return code
            except sqlite3.IntegrityError:
                continue
    raise PayPayError("coupon_issue", "クーポンを発行できませんでした。")


COUPON_PREFIX = {"free": "FR", "period": "PS", "pct30": "SP"}   # FR＝1回無料／PS＝期間無料（パス）／SP＝支援30%引き
UNLIMITED_DAYS = 36500                                          # 「無制限」の期間チケット（100年）


def coupon_issue_kind(kind: str, days: int, issued_order: str, note: str = "") -> tuple[str, float]:
    """購入（チケット・支援BOX）で発行するクーポン。同じ注文からは同じコードを返す（二重発行しない）。
    戻り値は (コード, 有効期限のUNIX時刻)。"""
    if kind not in COUPON_PREFIX:
        raise ValueError(kind)
    days = max(1, min(int(days), UNLIMITED_DAYS))
    note = str(note or "").strip()[:60] or None
    now = time.time()
    with _ledger() as db:
        _coupon_table(db)
        row = db.execute("SELECT code, expires FROM coupons WHERE issued_order=?", (issued_order,)).fetchone()
        if row:
            return row["code"], float(row["expires"])
        for _ in range(10):
            code = _new_coupon_code(COUPON_PREFIX[kind])
            try:
                db.execute("INSERT INTO coupons(code, created, expires, state, issued_order, used_order, updated, kind, note) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (code, now, now + days * 86400, "active", issued_order, None, now, kind, note))
                return code, now + days * 86400
            except sqlite3.IntegrityError:
                continue
    raise PayPayError("coupon_issue", "クーポンを発行できませんでした。")


def coupon_expires(code: str):
    """有効期限（UNIX時刻）。見つからなければ None。"""
    with _ledger() as db:
        _coupon_table(db)
        row = db.execute("SELECT expires FROM coupons WHERE code=?", (code,)).fetchone()
    return float(row["expires"]) if row else None


def coupon_kind(code: str):
    """クーポンの種類（'pct10' / 'free' / 'period' / 'pct30'）。見つからなければ None。"""
    with _ledger() as db:
        _coupon_table(db)
        row = db.execute("SELECT kind FROM coupons WHERE code=?", (code,)).fetchone()
    return (row["kind"] or "pct10") if row else None


def coupon_revoke(code: str) -> bool:
    """未使用の無料・期間・支援クーポンを取り消す（使用中・使用済みは取り消さない）。"""
    with _ledger() as db:
        _coupon_table(db)
        cur = db.execute("UPDATE coupons SET state='used', used_order='revoked', updated=? WHERE code=? AND state='active' AND kind IN ('free','period','pct30')",
                         (time.time(), code))
        return cur.rowcount == 1


def coupon_check(code: str):
    """(使えるか, 理由)。使えるのは有効期限内で、未使用かつ、ほかの注文で使用中でないもの。"""
    with _ledger() as db:
        _coupon_table(db)
        row = db.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
    if not row:
        return False, "クーポンコードが見つかりません。入力を確認してください。"
    if row["state"] == "used":
        return False, "このクーポンはすでに使われています。"
    if row["state"] == "reserved":
        return False, "このクーポンは、ほかのご注文で使用中です。"
    if time.time() > float(row["expires"]):
        return False, "このクーポンは有効期限が切れています。"
    return True, ""


def coupon_reserve(code: str, order_id: str) -> bool:
    with _ledger() as db:
        _coupon_table(db)
        cur = db.execute("UPDATE coupons SET state='reserved', used_order=?, updated=? WHERE code=? AND state='active' AND expires>?",
                         (order_id, time.time(), code, time.time()))
        return cur.rowcount == 1


def coupon_release(code: str, order_id: str) -> None:
    with _ledger() as db:
        _coupon_table(db)
        db.execute("UPDATE coupons SET state='active', used_order=NULL, updated=? WHERE code=? AND state='reserved' AND used_order=?",
                   (time.time(), code, order_id))


def coupon_consume(code: str, order_id: str) -> None:
    with _ledger() as db:
        _coupon_table(db)
        db.execute("UPDATE coupons SET state='used', used_order=COALESCE(used_order, ?), updated=? WHERE code=? AND state IN ('reserved','active')",
                   (order_id, time.time(), code))


def coupon_list(limit: int = 100):
    with _ledger() as db:
        _coupon_table(db)
        return [dict(r) for r in db.execute("SELECT * FROM coupons ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()]


async def refresh_session():
    """管理画面の「PayPayの接続を更新」: 保存済みの電話番号・パスワードでログインし直す（期限内でも）。
    SMS認証を求められたら otp_required（その後は管理画面のSMS認証欄で続きを入力できる）。"""
    with _LOCK:
        account = await _fresh_account(force=True, manual=True)
    return {"expires_at": int(account.get("expires_at", 0))}


def link_order_id(code: str):
    """台帳で、このリンクがどの注文のものとして記録されているか。"""
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        return None
    with _ledger() as db:
        row = db.execute("SELECT order_id FROM links WHERE digest=?", (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
    return row["order_id"] if row else None


def link_diag(code: str) -> str:
    """管理画面用: 台帳の状態と、最後にPayPayが返した状態名。"""
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        return ""
    try:
        with _ledger() as db:
            row = db.execute("SELECT state, last_status FROM links WHERE digest=?", (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
    except Exception:
        return ""
    return f"{row['state']} / {row['last_status'] or '-'}" if row else ""


def mark_paid_by_admin(code: str, purchase_id: str):
    """管理者がPayPayアプリで着金を確認したうえで、手動で支払い済みにする。"""
    if isinstance(code, str) and CODE_RE.fullmatch(code):
        with _ledger() as db:
            db.execute("UPDATE links SET state='paid' WHERE digest=? AND order_id=?", (hashlib.sha256(code.encode()).hexdigest(), purchase_id))


# ---------------------------------------------------------------- 注文の永続化（再起動しても支払い済み・保留の注文を失わない）
_ORDERS_FILE = _ROOT / "orders.enc"

def save_orders(data: dict):
    raw = _cipher().encrypt(json.dumps(data, ensure_ascii=False).encode())
    if github_runtime_store.enabled():
        try:
            github_runtime_store.write_blob("paypay/orders.enc", raw)
        except github_runtime_store.RuntimeStoreError as exc:
            raise PayPayError("runtime_store", str(exc)) from exc
        return
    _ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".orders-", dir=_ROOT)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, _ORDERS_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def load_orders() -> dict:
    try:
        raw = github_runtime_store.read_blob("paypay/orders.enc", _ORDERS_FILE)
    except github_runtime_store.RuntimeStoreError as exc:
        raise PayPayError("runtime_store", str(exc)) from exc
    if not raw:
        return {}
    try:
        data = json.loads(_cipher().decrypt(raw))
        return data if isinstance(data, dict) else {}
    except PayPayError:
        raise
    except Exception:
        raise PayPayError("orders_unreadable", "保存済みの注文を読み取れません。") from None
