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
import shop_storage
from shop_firestore_workflow import FirestoreWorkflowRepository

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
_ROOT = Path(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay")).resolve()
_ACCOUNT_FILE = _ROOT / "account.enc"
_LEDGER_FILE = Path(os.environ.get("PAYPAY_LEDGER_PATH", str(_ROOT / "payments.sqlite3")))
_LOCK = threading.Lock()
_pending_otp: dict = {}
_otp_workflow = None
_otp_workflow_lock = threading.Lock()
_OTP_SESSION_ID = "paypay-admin-session"
_logger = logging.getLogger(__name__)


def _otp_repository():
    global _otp_workflow
    if shop_storage.selected_backend() != "firestore":
        return None
    with _otp_workflow_lock:
        if _otp_workflow is None:
            _otp_workflow = FirestoreWorkflowRepository(shop_storage.firestore_client())
        return _otp_workflow


def _pending_otp_save(data: dict):
    repository = _otp_repository()
    if repository is None:
        _pending_otp.clear()
        _pending_otp.update(data)
        return
    encrypted = _cipher().encrypt(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    try:
        repository.save_otp_session(_OTP_SESSION_ID, encrypted, time.time() + 600)
    except Exception as exc:
        raise PayPayError("otp_store", "SMS認証状態を安全に保存できません。") from exc


def _pending_otp_load():
    repository = _otp_repository()
    if repository is None:
        return dict(_pending_otp)
    encrypted = repository.get_otp_session(_OTP_SESSION_ID)
    if not encrypted:
        return {}
    try:
        value = json.loads(_cipher().decrypt(encrypted.encode("ascii")))
    except Exception as exc:
        raise PayPayError("otp_store", "保存済みSMS認証状態を読み取れません。") from exc
    if not isinstance(value, dict):
        raise PayPayError("otp_store", "保存済みSMS認証状態が不正です。")
    return value


def _pending_otp_consume():
    """Atomically take the pending OTP state so concurrent submissions cannot replay it."""
    repository = _otp_repository()
    if repository is None:
        with _LOCK:
            if not _pending_otp:
                return {}
            value = dict(_pending_otp)
            _pending_otp.clear()
            return value
    try:
        encrypted = repository.consume_otp_session(_OTP_SESSION_ID)
    except Exception as exc:
        raise PayPayError("otp_store", "SMS認証状態を安全に取得できません。") from exc
    if not encrypted:
        return {}
    try:
        value = json.loads(_cipher().decrypt(encrypted.encode("ascii")))
    except Exception as exc:
        raise PayPayError("otp_store", "保存済みSMS認証状態を読み取れません。") from exc
    if not isinstance(value, dict):
        raise PayPayError("otp_store", "保存済みSMS認証状態が不正です。")
    return value


def _pending_otp_clear():
    repository = _otp_repository()
    if repository is None:
        _pending_otp.clear()
        return
    try:
        repository.clear_otp_session(_OTP_SESSION_ID)
    except Exception as exc:
        raise PayPayError("otp_store", "SMS認証状態を安全に消去できません。") from exc


def otp_pending() -> bool:
    return bool(_pending_otp_load())

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
        _pending_otp_save({
            "phone": phone, "password": password, "uuid": uuid,
            "otp_reference_id": result["otp_reference_id"],
            "otp_prefix": result["otp_prefix"],
            "cookies": result.get("cookies", {}),
        })
        return {"otp_required": True, "prefix": result["otp_prefix"]}
    if not result["session_cookie"]:
        raise PayPayError("cookie_missing", "PayPayログインは応答しましたが、受取セッションを確認できません。")
    _save_account({"phone":phone,"password":password,"uuid":uuid,
                   "access_token":result["access_token"],"session_cookie":result["session_cookie"],
                   "expires_at":time.time()+result["expires_in"]})
    _pending_otp_clear()
    return {"otp_required": False}

async def finish_login(otp: str):
    _cipher()
    if not OTP_RE.fullmatch(otp or ""):
        raise PayPayError("otp_format", "SMS認証コードは4桁で入力してください。")
    p = _pending_otp_consume()
    if not p:
        raise PayPayError("otp_expired", "SMS認証の有効期限が切れました。最初からログインしてください。")
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
    _pending_otp_clear()
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

def payment_link_digest(raw_link: str) -> str:
    """Return the stable SHA-256 digest used to prevent PayPay link reuse."""
    code = _normalize_link(raw_link)
    if not code:
        raise PayPayError("bad_link", "PayPay送金リンクの形式が正しくありません。")
    return hashlib.sha256(code.encode()).hexdigest()


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
    if not isinstance(p, dict) or p.get("orderStatus") != "PENDING":
        raise PayPayError("not_pending", "このPayPayリンクは未受取状態ではありません。")
    info, msg = p.get("pendingP2PInfo"), p.get("message")
    if not isinstance(info, dict) or not isinstance(msg, dict) or type(info.get("amount")) is not int or info["amount"] != amount:
        raise PayPayError("amount_mismatch", "PayPay送金金額と注文金額が一致しません。")
    if info.get("isLinkBlocked") is not False or info.get("isSetPasscode") is not False:
        raise PayPayError("link_restricted", "ブロック中またはパスコード付きリンクは利用できません。")
    expiry = _parse_time(info.get("expiredAt"))
    if not expiry or expiry <= datetime.now(timezone.utc):
        raise PayPayError("expired", "PayPayリンクの有効期限が切れているか確認できません。")
    sender, receiver = p.get("sender"), p.get("receiver")
    if not isinstance(sender, dict) or not isinstance(receiver, dict) or not sender.get("externalId") or not receiver.get("externalId") or sender["externalId"] == receiver["externalId"]:
        raise PayPayError("identity", "送金元・受取先を確認できません。")
    data = msg.get("data")
    if (not isinstance(data, dict) or not isinstance(info.get("orderId"), str) or not info["orderId"]
            or data.get("orderId") != info.get("orderId") or data.get("amount") != amount or data.get("status") != "PENDING"):
        raise PayPayError("order_mismatch", "送金リンクの注文情報が一致しません。")
    if not all(isinstance(msg.get(k), str) and msg[k] for k in ("messageId", "chatRoomId")) or not _parse_time(data.get("transactionAt")):
        raise PayPayError("link_incomplete", "送金リンクの取引情報が不足しています。")
    return {"order_id":info["orderId"],"message_id":msg["messageId"],"chat_id":msg["chatRoomId"],
            "request_at":data["transactionAt"]}

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
            if not github_runtime_store.enabled():
                os.chmod(_LEDGER_FILE, 0o600)
            yield db
    except github_runtime_store.RuntimeStoreError as exc:
        raise PayPayError("runtime_store", str(exc)) from exc

async def receive_payment(raw_link: str, amount: int, purchase_id: str, *, external_ledger=False):
    code = _normalize_link(raw_link)
    if not code: raise PayPayError("bad_link", "PayPay送金リンクの形式が正しくありません。")
    with _LOCK:
        account = _load_account()
        if not account:
            raise PayPayError("login_required", "管理者のPayPay接続が必要です。")
        if float(account.get("expires_at",0)) <= time.time()+60 or not account.get("session_cookie"):
            renewed = await _oauth({"scope":"SIGN_IN","client_uuid":account["uuid"],"grant_type":"password",
                                    "username":account["phone"],"password":account["password"],
                                    "add_otp_prefix":True,"language":"ja"},
                                   "https://www.paypay.ne.jp/app/account/sign-in")
            if renewed["kind"] == "otp":
                _pending_otp_save({
                    "phone": account["phone"], "password": account["password"],
                    "uuid": account["uuid"],
                    "otp_reference_id": renewed["otp_reference_id"],
                    "otp_prefix": renewed["otp_prefix"],
                    "cookies": renewed.get("cookies", {}),
                })
                raise PayPayError("otp_required", "PayPayがSMS認証を要求しました。管理者画面で認証コードを入力してください。")
            if not renewed.get("session_cookie"):
                raise PayPayError("cookie_missing", "PayPayの再認証結果を確認できません。")
            account.update(access_token=renewed["access_token"],session_cookie=renewed["session_cookie"],
                           expires_at=time.time()+renewed["expires_in"])
            _save_account(account)
        referer=f"https://www.paypay.ne.jp/app/p2p/{code}?pid=SMS&link_key={code}&af_dp=paypay%3A%2F%2Fpending%2Fdetail%3Fsource%3Dsns_link"
        headers=_headers(referer)
        timeout=aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout,headers=headers,cookies={"token":account["session_cookie"]}) as session:
            try:
                async with session.get(LINK_CHECK_ENDPOINT,params={"verificationCode":code,"client_uuid":account["uuid"]}) as r:
                    if r.status != 200: raise PayPayError("link_check", "PayPayリンクを確認できませんでした。")
                    body=await r.json(content_type=None)
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError):
                raise PayPayError("link_network", "PayPayリンク確認で通信エラーが発生しました。") from None
            verified=_verify_link(body,amount)
            digest=hashlib.sha256(code.encode()).hexdigest()
            if external_ledger:
                # Preserve duplicate protection for links recorded before the
                # Firestore migration. The GitHub-backed legacy ledger is read-only
                # in this mode; new attempts are written only to Firestore.
                with _ledger() as db:
                    if db.execute("SELECT 1 FROM links WHERE digest=?", (digest,)).fetchone():
                        raise PayPayError("duplicate", "このPayPayリンクは移行前に使用されています。")
            else:
                with _ledger() as db:
                    try: db.execute("INSERT INTO links(digest,order_id,state,created) VALUES(?,?,?,?)",(digest,purchase_id,"attempted",time.time()))
                    except sqlite3.IntegrityError:
                        raise PayPayError("duplicate","このPayPayリンクまたは注文は既に処理されています。") from None
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
                if not external_ledger:
                    with _ledger() as db: db.execute("UPDATE links SET state='review' WHERE digest=?",(digest,))
                raise PayPayError("accept_uncertain","受取結果が不明です。同じリンクを再送せず、管理者へお問い合わせください。") from None
            ok=(status_code==200 and isinstance(reply,dict) and isinstance(reply.get("header"),dict)
                and reply["header"].get("resultCode")=="S0000" and isinstance(reply.get("payload"),dict)
                and reply["payload"].get("orderStatus")=="COMPLETED" and reply.get("error") is None)
            if ok:
                if not external_ledger:
                    with _ledger() as db: db.execute("UPDATE links SET state='paid' WHERE digest=?",(digest,))
                return digest
            hold_code = _provider_hold_code(reply)
            state="pending" if hold_code else "review"
            if not external_ledger:
                with _ledger() as db: db.execute("UPDATE links SET state=? WHERE digest=?",(state,digest))
            raise PayPayError("temporary_hold" if state=="pending" else "accept_unconfirmed",
                              "PayPay側で一時保留です。状態を確認するまで再送金しないでください。" if state=="pending"
                              else "受取結果を確定できません。再購入せず管理者へお問い合わせください。")
