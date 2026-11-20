#!/usr/bin/env python3
"""
kyash_api.py -- Kyash (api.kyash.me) の API層

仕様の要点:
  ・ベースURL      : https://api.kyash.me
  ・認証           : 全APIに共通ヘッダ x-auth: <token>  (Bearerではない)
  ・端末識別       : x-kyash-client-id / installation-id / android-id / adid を毎回送る
                     (アカウントごとにランダム生成して固定で使い回す)
  ・レスポンス包み : {code, detailCode, result:{data: ...}}  成否は HTTP 2xx で判定
  ・冪等キー       : 送金系は uuidClient (クライアント生成UUID) を付ける
  ・ログイン       : POST /v2/login {email,password}
                       → verificationMethod == "link" ならメール/SMSのリンクで
                         POST /v2/login/link/verify {token}
                     同じ端末IDで入り直すと2回目以降は検証が省かれる (信頼端末)
  ・トークン       : 約30日。POST /v1/session/refresh {refresh_token} で更新 (回転する)

使うエンドポイント:
  自分     GET  /v1/me
  残高     GET  /v2/me/wallet/balance
  上限     GET  /v1/transactions/available_amount
  明細     GET  /v1/me/wallets/{walletUuid}/timeline?limit=N
  送金     POST /v1/transactions
  リンク   POST /v1/me/links  /  DELETE /v1/links/{uuid}  /  PUT /v1/links/{uuid}/receive
  リンク解決  kyash.me/payments/<code> の公開ページ (HTML) から金額とUUIDを読む
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from uuid import UUID, uuid4, uuid5

import requests

BASE_DIR = os.environ.get("EXCHANGE_DATA_DIR", "/var/lib/paykyash")
CONFIG_FILE = os.path.join(BASE_DIR, "kyash_config.json")      # 任意 (ヘッダの上書き用)
ACCOUNTS_FILE = os.path.join(BASE_DIR, "kyash_accounts.json")

BASE_URL = "https://api.kyash.me"
AUTH_HEADER = "x-auth"
TIMEOUT = 30

_DEFAULT_STATIC = {
    "x-kyash-client-version": "13.1.1",
    "x-kyash-device-info": "Pixel 6a,16,Android",
    "x-kyash-os": "android",
    "x-kyash-device-language": "ja",
}

_DEBUG = os.environ.get("KYASH_DEBUG", "").strip() == "1"


class KyashError(Exception):
    def __init__(self, status, code=None, detail=None, message=None, body=None):
        self.status = status
        self.code = code
        self.detail = detail
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status} code={code} detail={detail} msg={message}")

    def text(self) -> str:
        """利用者・ログに出してよい説明 (トークン等は含まない)"""
        if self.message:
            return f"{self.message} (HTTP {self.status})" if self.status else str(self.message)
        return f"HTTP {self.status}" + (f" / code {self.code}" if self.code else "")


class KyashNetworkError(KyashError):
    """通信の途中で切れた。maybe_sent=True ならサーバーに届いた可能性がある。

    送金を投げた後のタイムアウトは「失敗」ではない。失敗扱いで投げ直すと
    二重送金になるので、呼び出し側は成否不明として扱うこと。
    """

    def __init__(self, message, maybe_sent=True):
        super().__init__(0, message=message)
        self.maybe_sent = maybe_sent


# ------------------------------------------------------------------
# JSON 入出力
# ------------------------------------------------------------------

def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _atomic_dump(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config():
    cfg = _read_json(CONFIG_FILE, {}) or {}
    static = dict(_DEFAULT_STATIC)
    static.update(cfg.get("static_headers", {}) or {})
    return {"static_headers": static, "auth_header": cfg.get("auth_header", AUTH_HEADER)}


def gen_device():
    """新しい端末として振る舞うための識別子一式を生成"""
    return {
        "x-kyash-client-id": str(uuid4()),
        "x-kyash-installation-id": str(uuid4()),
        "x-kyash-android-id": secrets.token_hex(8),
        "x-kyash-adid": str(uuid4()),
    }


# ------------------------------------------------------------------
# アカウント / トークン保存
#   kyash_accounts.json:
#     {"active": "<email>", "accounts": {"<email>": {email, token, refreshToken,
#       tokenExpiry, publicId, userName, device:{...}}}}
# ------------------------------------------------------------------

def load_accounts():
    data = _read_json(ACCOUNTS_FILE, None)
    if not isinstance(data, dict):
        data = {"active": None, "accounts": {}}
    data.setdefault("active", None)
    data.setdefault("accounts", {})
    return data


def save_accounts(data):
    _atomic_dump(ACCOUNTS_FILE, data)


def get_active_email():
    return load_accounts().get("active")


def get_account(email=None):
    data = load_accounts()
    email = email or data.get("active")
    if not email:
        return None
    return data["accounts"].get(email)


def get_device_for_account(email):
    """そのアカウント用の端末ID。既に保存されていれば必ず再利用 (= 検証スキップを維持)"""
    data = load_accounts()
    acc = data["accounts"].get(email)
    if acc and acc.get("device"):
        return acc["device"]
    dev = gen_device()
    acc = data["accounts"].setdefault(email, {"email": email})
    acc["device"] = dev
    save_accounts(data)
    return dev


def upsert_account(email, token, refresh_token=None, token_expiry=None,
                   device=None, public_id=None, user_name=None, make_active=True):
    data = load_accounts()
    acc = data["accounts"].setdefault(email, {"email": email})
    acc["email"] = email
    acc["token"] = token
    if refresh_token is not None:
        acc["refreshToken"] = refresh_token
    if token_expiry is not None:
        acc["tokenExpiry"] = token_expiry
    if device is not None:
        acc["device"] = device
    if public_id is not None:
        acc["publicId"] = public_id
    if user_name is not None:
        acc["userName"] = user_name
    if make_active:
        data["active"] = email
    save_accounts(data)
    return acc


def remove_tokens():
    """保存済みトークンを消す (端末IDは残す = 次も同じ端末として入れる)。消したメールを返す。"""
    data = load_accounts()
    removed = []
    for email, acc in data["accounts"].items():
        if acc.get("token") or acc.get("refreshToken"):
            for k in ("token", "refreshToken", "tokenExpiry", "password"):
                acc.pop(k, None)
            removed.append(email)
    save_accounts(data)
    return removed


def is_ready():
    acc = get_account()
    return bool(acc and acc.get("token"))


# ------------------------------------------------------------------
# HTTP コア
# ------------------------------------------------------------------

def make_headers(token=None, device=None, content_type=None):
    cfg = load_config()
    h = dict(cfg["static_headers"])
    if device:
        h.update(device)
    h["x-kyash-date"] = str(int(time.time()))
    h["x-kyash-device-uptime-mills"] = str(int(time.monotonic() * 1000) + 100000000)
    h["accept"] = "application/json"
    h["accept-encoding"] = "gzip"
    h["user-agent"] = "okhttp/5.3.2"
    if token:
        h[cfg["auth_header"]] = token
    if content_type:
        h["content-type"] = content_type
    return h


def _request(method, path, token=None, device=None, json_body=None, params=None):
    """api.kyash.me を叩いて result.data を返す。失敗は KyashError / KyashNetworkError。"""
    url = BASE_URL + path if path.startswith("/") else path
    ct = "application/json; charset=UTF-8" if json_body is not None else None
    headers = make_headers(token=token, device=device, content_type=ct)
    try:
        r = requests.request(method, url, headers=headers, json=json_body,
                             params=params, timeout=TIMEOUT)
    except requests.exceptions.ConnectTimeout as e:
        raise KyashNetworkError(f"接続できない: {type(e).__name__}", maybe_sent=False)
    except requests.exceptions.ConnectionError as e:
        # 名前解決や接続拒否は「届いていない」と言い切れる。それ以外 (途中切断) は不明
        txt = str(e)
        sent = not any(w in txt for w in ("NameResolution", "Failed to establish",
                                          "getaddrinfo", "Connection refused"))
        raise KyashNetworkError(f"通信失敗: {type(e).__name__}", maybe_sent=sent)
    except requests.exceptions.RequestException as e:
        raise KyashNetworkError(f"通信失敗: {type(e).__name__}", maybe_sent=True)
    try:
        body = r.json()
    except ValueError:
        body = None
    if _DEBUG:
        print(f"[kyash] {method} {path} → {r.status_code}")
    if not (200 <= r.status_code < 300):
        code = detail = message = None
        if isinstance(body, dict):
            code = body.get("code")
            detail = body.get("detailCode")
            message = body.get("error") or body.get("message")
            if isinstance(body.get("result"), dict):
                d = body["result"].get("data")
                if isinstance(d, dict):
                    message = message or d.get("message")
        raise KyashError(r.status_code, code, detail, message, body)
    if isinstance(body, dict) and isinstance(body.get("result"), dict):
        return body["result"].get("data")
    return body


# ------------------------------------------------------------------
# ログイン
# ------------------------------------------------------------------

def login_password(email, password, device=None):
    """ステップ1: メール+パスワード。
    戻り値: {verified, verificationMethod, token, refreshToken, tokenExpiry, device}
      verified=True  … 検証不要 (信頼端末)。token がそのまま使える
      verified=False … リンク検証が必要 (メール/SMSのリンクを login_verify_link へ)"""
    device = device or get_device_for_account(email)
    data = _request("POST", "/v2/login", device=device,
                    json_body={"email": email, "password": password}) or {}
    method = data.get("verificationMethod")
    need = str(method).lower() == "link"
    return {"verified": not need, "verificationMethod": method,
            "token": data.get("token"), "refreshToken": data.get("refreshToken"),
            "tokenExpiry": data.get("tokenExpiry"), "device": device}


def extract_link_token(pasted):
    """貼られたマジックリンク or 生トークンから、検証用トークンを取り出す"""
    s = (pasted or "").strip()
    if not s:
        return ""
    if "://" in s or s.startswith("http"):
        try:
            u = urlparse(s)
            q = parse_qs(u.query)
            for key in ("token", "code", "t", "verificationToken"):
                if q.get(key):
                    return q[key][0]
            seg = [x for x in u.path.split("/") if x]
            if seg:
                return seg[-1]
        except ValueError:
            pass
    return s


def login_verify_link(pasted, provisional_token=None, device=None):
    """ステップ2: リンク (またはトークン) で検証してログイン完了"""
    tok = extract_link_token(pasted) or provisional_token
    if not tok:
        raise KyashError(0, message="検証トークンが空")
    try:
        data = _request("POST", "/v2/login/link/verify", device=device,
                        json_body={"token": tok})
    except KyashError:
        if provisional_token and tok != provisional_token:
            data = _request("POST", "/v2/login/link/verify", device=device,
                            json_body={"token": provisional_token})
        else:
            raise
    data = data or {}
    return {"token": data.get("token"), "refreshToken": data.get("refreshToken"),
            "tokenExpiry": data.get("tokenExpiry")}


def finalize_login(email, token, refresh_token, token_expiry, device):
    """トークンを保存し、/v1/me で本人情報を補完して active にする"""
    public_id = user_name = None
    try:
        me = get_me(token, device) or {}
        public_id = me.get("publicId")
        user_name = me.get("userName")
    except KyashError:
        pass
    return upsert_account(email, token, refresh_token, token_expiry,
                          device=device, public_id=public_id, user_name=user_name)


def refresh_session(email=None):
    """POST /v1/session/refresh。body は snake_case の refresh_token。新トークンを保存する。"""
    data_acc = load_accounts()
    email = email or data_acc.get("active")
    acc = data_acc["accounts"].get(email) if email else None
    if not acc or not acc.get("refreshToken"):
        raise KyashError(0, message="refreshToken が無い (未ログイン)")
    device = acc.get("device") or gen_device()
    data = _request("POST", "/v1/session/refresh", token=acc.get("token"), device=device,
                    json_body={"refresh_token": acc["refreshToken"]}) or {}
    new_tok = data.get("token")
    if not new_tok:
        raise KyashError(0, message="更新レスポンスに token が無い")
    upsert_account(email, new_tok, data.get("refreshToken") or acc["refreshToken"],
                   data.get("tokenExpiry"), device=device, public_id=acc.get("publicId"),
                   user_name=acc.get("userName"), make_active=(data_acc.get("active") == email))
    d = load_accounts()
    if email in d["accounts"]:
        d["accounts"][email]["lastRefreshAt"] = time.time()
        save_accounts(d)
    return {"tokenExpiry": data.get("tokenExpiry")}


def parse_time(s):
    """ISO文字列 or epoch → epoch秒。ナノ秒の小数も許容。読めなければ None。"""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / 1000 if float(s) > 1e12 else float(s)
    s = str(s)
    m = re.match(r"(.*\.\d{6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    for cand in (s, s.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(cand).timestamp()
        except ValueError:
            continue
    return None


def token_remaining_seconds(email=None):
    ts = parse_time((get_account(email) or {}).get("tokenExpiry"))
    return None if ts is None else ts - time.time()


def ensure_fresh(min_hours=72, min_gap_hours=24, email=None):
    """期限が迫っていて、前回更新から間が空いている時だけ refresh する (無駄打ちしない)。
    戻り: (changed, remaining_hours, action)  action: none/refreshed/need_login/too_soon"""
    rem = token_remaining_seconds(email)
    acc = get_account(email) or {}
    last = acc.get("lastRefreshAt") or 0
    if rem is None or rem <= 0:
        if alive_check(email):
            return False, rem, "none"
        try:
            refresh_session(email)
            return True, token_remaining_seconds(email), "refreshed"
        except KyashError:
            return False, rem, "need_login"
    if rem < min_hours * 3600:
        if (time.time() - last) < min_gap_hours * 3600:
            return False, rem / 3600, "too_soon"
        refresh_session(email)
        rem = token_remaining_seconds(email)
        return True, (rem / 3600 if rem is not None else None), "refreshed"
    return False, rem / 3600, "none"


# ------------------------------------------------------------------
# 取得系
# ------------------------------------------------------------------

def _auth(email=None):
    acc = get_account(email)
    if not acc or not acc.get("token"):
        raise KyashError(0, message="Kyash未ログイン (/kyashログイン で入って)")
    return acc["token"], acc.get("device") or gen_device()


def get_me(token=None, device=None, email=None):
    if token is None:
        token, device = _auth(email)
    return _request("GET", "/v1/me", token=token, device=device)


def get_balance(token=None, device=None, email=None):
    """残高 {amount, kyashMoney, kyashValue, temporaryValue, updatedAt}"""
    if token is None:
        token, device = _auth(email)
    w = _request("GET", "/v2/me/wallet/balance", token=token, device=device) or {}
    bd = w.get("amountBreakdown", {}) or {}
    return {"amount": w.get("amount"), "kyashMoney": bd.get("kyashMoney"),
            "kyashValue": bd.get("kyashValue"), "temporaryValue": bd.get("temporaryValue"),
            "updatedAt": w.get("updatedAt")}


def get_available_amount(token=None, device=None, email=None):
    if token is None:
        token, device = _auth(email)
    return _request("GET", "/v1/transactions/available_amount", token=token, device=device)


def get_primary_wallet(token=None, device=None, email=None):
    if token is None:
        token, device = _auth(email)
    return _request("GET", "/v1/me/primary_wallet", token=token, device=device)


_wallet_cache: dict[str, str] = {}


def get_timeline(limit=20, token=None, device=None, email=None):
    """取引明細 (GET /v1/me/wallets/{walletUuid}/timeline)。1件は {"transfer": {...}} の形。"""
    if token is None:
        token, device = _auth(email)
    wallet_uuid = _wallet_cache.get(token)
    if not wallet_uuid:
        wallet_uuid = (get_primary_wallet(token, device) or {}).get("uuid")
        if not wallet_uuid:
            raise KyashError(0, message="ウォレットUUIDを取得できない")
        _wallet_cache.clear()
        _wallet_cache[token] = wallet_uuid
    data = _request("GET", f"/v1/me/wallets/{wallet_uuid}/timeline",
                    token=token, device=device, params={"limit": limit}) or {}
    return data.get("timelines", []) if isinstance(data, dict) else []


def alive_check(email=None):
    try:
        token, device = _auth(email)
        return bool(_request("GET", "/v1/me", token=token, device=device))
    except KyashError:
        return False


# ------------------------------------------------------------------
# 送金・リンク
# ------------------------------------------------------------------

ACTION_SEND = 1
LINK_ACTION_SEND = 1


def send_money(amount, public_id_to, username_to, token=None, device=None, email=None, *, request_id=None):
    """直接送金 (POST /v1/transactions action:1)。戻り値: {group, tx, amount}"""
    if token is None:
        token, device = _auth(email)
    amount = int(amount)
    if amount <= 0:
        raise KyashError(0, message="金額は1円以上")
    request_id = request_id or str(uuid4())
    tx = {"action": ACTION_SEND, "amount": amount, "currency": "JPY",
          "publicId_to": int(public_id_to), "username_to": username_to,
          "uuidClient": request_id}
    data = _request("POST", "/v1/transactions", token=token, device=device,
                    json_body={"transaction": [tx], "uuidClient": str(uuid5(UUID(request_id), "group"))})
    group = txid = None
    for item in (data or []):
        if isinstance(item, dict):
            if item.get("type") == "transactionGroup":
                group = item.get("uuidAPI")
            elif item.get("type") == "transaction":
                txid = item.get("uuidAPI")
    return {"group": group, "tx": txid, "amount": amount}


def create_send_link(amount, token=None, device=None, email=None, *, request_id=None):
    """送金リンク作成 (POST /v1/me/links, action=1)。戻り値: (リンクURL, data)"""
    if token is None:
        token, device = _auth(email)
    amount = int(amount)
    if amount <= 0:
        raise KyashError(0, message="金額は1円以上")
    body = {"action": LINK_ACTION_SEND, "amount": amount, "repeatable": False,
            "service": "share", "uuidClient": request_id or str(uuid4())}
    data = _request("POST", "/v1/me/links", token=token, device=device, json_body=body) or {}
    return data.get("link"), data


def looks_like_uuid(s):
    s = str(s or "")
    parts = s.split("-")
    return len(parts) == 5 and len(s) == 36 and all(
        c in "0123456789abcdefABCDEF" for c in s if c != "-")


def _html_text(fragment: str) -> str:
    import html as _html
    s = re.sub(r"<[^>]+>", "", fragment or "")
    return _html.unescape(s).strip()


def parse_link_sender(html: str) -> dict:
    """公開ページに出ている送信者 (リンクを作った人)。

        <img class="profilePicture" src="https://image.kyash.me/<uuid>.jpg?...">
        <p class="realName">表示名</p>
        <p class="userid"><span>ID：</span>kyash_id</p>

    Kyash ID (userid) は一意。表示名は自由に変えられるので照合には使わない。
    """
    out = {"sender_name": "", "sender_user_name": "", "sender_icon": ""}
    m = re.search(r'class="realName"[^>]*>(.*?)</p>', html or "", re.S)
    if m:
        out["sender_name"] = _html_text(m.group(1))[:80]
    m = re.search(r'class="userid"[^>]*>(.*?)</p>', html or "", re.S)
    if m:
        inner = re.sub(r"<span[^>]*>.*?</span>", "", m.group(1), flags=re.S)
        uid = _html_text(inner).lstrip("@").strip()
        if re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", uid or ""):
            out["sender_user_name"] = uid
    m = re.search(r'class="profilePicture"[^>]*src="https?://image\.kyash\.me/([0-9a-f-]{36})',
                  html or "")
    if m:
        out["sender_icon"] = m.group(1)
    return out


def resolve_kyash_link(url_or_code):
    """kyash.me/payments/<code> の公開ページ (HTML) から
    {amount, uuid, url, code, http_status, sender_name, sender_user_name, sender_icon}。

    受取UUIDは api.kyash.me では短縮コードから解決できない。公開ページに金額と
    リンクUUIDと送信者が埋まっているのでそれを読む (認証不要・受け取らない)。
    受取済み・取消済みのリンクはページにUUIDが出ない。
    """
    s = (url_or_code or "").strip()
    code = s.rstrip("/").rsplit("/", 1)[-1] if "/" in s else s
    code = code.split("?")[0].split("#")[0]
    url = f"https://kyash.me/payments/{code}"
    try:
        r = requests.get(url, headers={
            "user-agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
            "accept": "text/html"}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise KyashNetworkError(f"リンクページを開けない: {type(e).__name__}", maybe_sent=False)
    # ページは <meta charset="utf-8">。ヘッダに charset が無いと requests は
    # ISO-8859-1 で読んで表示名が化けるので明示する
    r.encoding = "utf-8"
    html = r.text or ""
    m = (re.search(r"amountText[^>]*>\s*¥?\s*([\d,]+)", html)
         or re.search(r"¥\s*([\d,]+)", html))
    amount = int(m.group(1).replace(",", "")) if m else None
    all_uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", html)
    img_uuids = set(re.findall(r"image\.kyash\.me/([0-9a-f-]{36})", html))
    cand = [u for u in all_uuids if u not in img_uuids]
    uuid = cand[0] if cand else None
    return {"amount": amount, "uuid": uuid, "url": url, "code": code,
            "http_status": r.status_code, **parse_link_sender(html)}


def claim_link(link_uuid, token=None, device=None, email=None):
    """送られてきた送金リンクを受け取る (PUT /v1/links/{uuid}/receive)。UUIDで渡すこと。"""
    if token is None:
        token, device = _auth(email)
    if not looks_like_uuid(link_uuid):
        raise KyashError(0, message="リンクUUIDの形式が違う")
    return _request("PUT", f"/v1/links/{link_uuid}/receive", token=token, device=device)


def delete_link(link_uuid, token=None, device=None, email=None):
    """自分が作った送金リンクを取り消す (DELETE /v1/links/{uuid})"""
    if token is None:
        token, device = _auth(email)
    if not looks_like_uuid(link_uuid):
        raise KyashError(0, message="リンクUUIDの形式が違う")
    return _request("DELETE", f"/v1/links/{link_uuid}", token=token, device=device)
