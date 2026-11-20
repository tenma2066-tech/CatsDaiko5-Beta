"""
PayPay API モジュール (paypay_api.py)
- 既存セッションの更新
- セッション管理 (tokens.json + accounts.json + udid_map.json)
- 残高取得 / 取引履歴 / 送金 / 請求リンク / 送金リンク / ユーザー検索

依存: httpx
paypay_bridge.py / paypay_login.py から import して使用
"""

import base64
import hashlib
import json
import os
import secrets
import time
from uuid import uuid4
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

import httpx


# VPS の永続データ保存先
_MODULE_DIR = os.environ.get("EXCHANGE_DATA_DIR", "/var/lib/paykyash")


# ============================================================
# 設定
# ============================================================

BASE_PORTAL = "https://www.paypay.ne.jp"
BASE_APP4 = "https://app4.paypay.ne.jp"
CLIENT_ID = "pay2-mobile-app-client"
TOKEN_FILE = os.path.join(_MODULE_DIR, "paypay_tokens.json")
ACCOUNTS_FILE = os.path.join(_MODULE_DIR, "paypay_accounts.json")
UDID_MAP_FILE = os.path.join(_MODULE_DIR, "paypay_udid_map.json")
JST = timezone(timedelta(hours=9))

# 応答の全文・リンクコード・認証コードはコンソールに出さない。受け取っていない
# 送金リンクは相手の手元で生きていて、コードを見られると他人に受け取られる。
# 調べる時だけ環境変数 PAYPAY_DEBUG=1 で出す。
_DEBUG = os.environ.get("PAYPAY_DEBUG", "").strip() == "1"


def _dbg(*a, **kw):
    if _DEBUG:
        print(*a, **kw)


def _mask(code) -> str:
    s = str(code or "")
    return f"{s[:4]}…" if s else ""

# UIカラーパレット (カテゴリ別)
COLOR = {
    "login_wait": 0x74B9FF,   # SMS待ち / 入力待ち (空色)
    "login_ok":   0x00B894,   # ログイン成功 (ミント)
    "balance":    0xFDCB6E,   # 残高 (ゴールド)
    "profile":    0x00CEC9,   # プロフィール (シアン)
    "send":       0xE17055,   # 送金 (テラコッタ)
    "receive":    0x55EFC4,   # 受取 (ターコイズ)
    "request":    0xFD79A8,   # 請求 (ピンク)
    "link_info":  0xA29BFE,   # 情報照会 (ラベンダー)
    "search":     0x6C5CE7,   # 検索 (パープル)
    "manage":     0xB2BEC3,   # アカウント管理 (グレー)
    "alive":      0x81ECEC,   # alive (薄シアン)
    "history":    0xDFE6E9,   # 履歴 (白寄り)
    "error":      0xD63031,   # エラー (クリムゾン)
    "warn":       0xFAB005,   # 警告 (アンバー)
    "ok":         0x00C853,   # 成功 (グリーン)
}

UA = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 6a Build/CP1A.260305.018; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/147.0.7727.138 Mobile Safari/537.36 jp.pay2.app.android/5.50.0"
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Client-Id": CLIENT_ID,
    "Client-OS-Type": "ANDROID",
    "Client-OS-Version": "36.0.0",
    "Client-OS-Release-Version": "16",
    "Client-Type": "PAYPAYAPP",
    "Client-Version": "5.72.2",
    "Client-Mode": "NORMAL",
    "Client-UUID": "15b77638-e9e9-4354-b43e-1c40fcf3bef0",
    "Device-UUID": "b57f484e-893d-3cdb-bf3f-831525685afc",
    "Device-Name": "Pixel 6a",
    "Device-Hardware-Name": "bluejay",
    "Device-Manufacturer-Name": "Google",
    "Device-Brand-Name": "google",
    "Is-Emulator": "false",
    "Network-Status": "WIFI",
    "System-Locale": "ja",
    "Timezone": "Asia/Tokyo",
    "User-Agent": "PaypayApp/5.72.2 Android16",
    "Device-Lock-Type": "DEVICE",
    "Device-Lock-App-Setting": "false",
    "Device-In-Call": "false",
    "Device-Screen-Recording": "false",
    "App-Mode": "domestic_automatic",
    "Device-App-Check": "FAILURE - UNKNOWN",
    "Device-Acceleration": "-0.0040711286_3.9784907E-4_-0.037927248",
    "Device-Acceleration-2": "NULL",
    "Device-Orientation": "-0.7248919_-0.00937534_0.010662667",
    "Device-Orientation-2": "NULL",
    "Device-Rotation": "0.74846226_-0.6630254_0.014197531",
    "Device-Rotation-2": "NULL",
    "Accept-Charset": "UTF-8",
    "Accept-Encoding": "gzip",
}


# ============================================================
# 汎用ユーティリティ
# ============================================================

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def update_cookies(cookies, resp):
    for h in resp.headers.get_list("set-cookie"):
        parts = h.split(";")
        nv = parts[0].split("=", 1)
        if len(nv) != 2:
            continue
        name, val = nv[0].strip(), nv[1].strip()
        if "1970" in h or val == "":
            cookies.pop(name, None)
        else:
            cookies[name] = val


def cookie_str(cookies):
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def deep_find(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            r = deep_find(v, *keys)
            if r is not None:
                return r
    return None


def extract_code_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    codes = params.get("code", [])
    return codes[0] if codes else None


def make_headers(device_uuid=None):
    h = {**HEADERS}
    if device_uuid:
        h["Device-UUID"] = device_uuid
    return h


def jst_now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def yen(n):
    try:
        return f"¥{int(n):,}"
    except Exception:
        return f"¥{n}"


# ============================================================
# アカウント管理（マルチアカウント / 永続化）
# ============================================================

def _mask_phone(phone) -> str:
    """電話番号をログや画面に出す時のマスク（末尾4桁のみ）。"""
    p = str(phone or "")
    return ("*" * max(0, len(p) - 4)) + p[-4:] if len(p) > 4 else "****"


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"active": None, "accounts": {}}


def _atomic_dump(path, data):
    """一時ファイルへ書いてから置換する。

    直接 open(..., "w") すると、書き込みの途中で落ちた瞬間にファイルが
    truncate されたまま残る。ここにはPayPayのトークンが入っており、
    壊れると再ログインするまで決済が全部止まる。
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[paypay] ❌ 保存失敗 {os.path.basename(path)}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def save_accounts(data):
    return _atomic_dump(ACCOUNTS_FILE, data)


def load_tokens():
    accounts = load_accounts()
    active_phone = accounts.get("active")
    if active_phone and active_phone in accounts.get("accounts", {}):
        return accounts["accounts"][active_phone]
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_tokens(access_token, refresh_token, phone="", device_uuid=""):
    accounts = load_accounts()
    if not phone:
        phone = accounts.get("active") or "unknown"
    account_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "phone": phone,
        "device_uuid": device_uuid or HEADERS.get("Device-UUID", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    accounts.setdefault("accounts", {})[phone] = account_data
    if not accounts.get("active"):
        accounts["active"] = phone
    save_accounts(accounts)
    _atomic_dump(TOKEN_FILE, account_data)
    print(f"[+] トークン保存: {os.path.basename(ACCOUNTS_FILE)} "
          f"(phone={_mask_phone(phone)})")


def switch_account(phone):
    accounts = load_accounts()
    if phone not in accounts.get("accounts", {}):
        print(f"[!] アカウント {phone} が見つかりません")
        print(f"    登録済み: {list(accounts.get('accounts', {}).keys())}")
        return False
    accounts["active"] = phone
    save_accounts(accounts)
    with open(TOKEN_FILE, "w") as f:
        json.dump(accounts["accounts"][phone], f, indent=2, ensure_ascii=False)
    print(f"[+] アクティブアカウント切替: {phone}")
    return True


# ============================================================
# UDID-電話番号 紐付け管理 (paypay_udid_map.json)
# ============================================================

def load_udid_map():
    """UDID-電話番号紐付けマップを読み込み
    構造: {"phone_to_udid": {"090xxxx": "uuid-..."}, "udid_to_phone": {"uuid-...": "090xxxx"}}"""
    try:
        with open(UDID_MAP_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"phone_to_udid": {}, "udid_to_phone": {}}


def save_udid_map(data):
    with open(UDID_MAP_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def generate_new_udid():
    """新しいランダムUDID (UUID v4) を生成"""
    return str(uuid4())


def get_udid_for_phone(phone, new_device=False):
    """電話番号に対応するUDIDを取得
    - new_device=True: 新規UDID生成して紐付け保存
    - new_device=False: 既存紐付けがあればそれを使う、なければ新規生成して保存
    戻り値: (udid, is_new)"""
    udid_map = load_udid_map()
    p2u = udid_map.setdefault("phone_to_udid", {})
    u2p = udid_map.setdefault("udid_to_phone", {})

    existing_udid = p2u.get(phone)
    if existing_udid and not new_device:
        print(f"[+] 既存UDID使用: phone={phone} → udid={existing_udid[:16]}...")
        return existing_udid, False

    # 新規生成
    new_udid = generate_new_udid()
    p2u[phone] = new_udid
    u2p[new_udid] = phone
    save_udid_map(udid_map)
    print(f"[+] 新規UDID生成: phone={phone} → udid={new_udid[:16]}...")
    return new_udid, True


def get_phone_for_udid(udid):
    """UDIDから電話番号を逆引き"""
    udid_map = load_udid_map()
    return udid_map.get("udid_to_phone", {}).get(udid)


# ============================================================
def refresh_access_token(refresh_token, device_uuid=None):
    print(f"\n[*] refresh_token更新...")
    headers = make_headers(device_uuid)
    session = httpx.Client(follow_redirects=False, timeout=30.0)
    r = session.post(
        f"{BASE_APP4}/bff/v2/oauth2/refresh",
        params={"payPayLang": "ja"},
        data={
            "clientId": CLIENT_ID,
            "grantType": "refresh_token",
            "refreshToken": refresh_token,
            "code": refresh_token,
            "redirectUri": "paypay://oauth2/callback",
        },
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        data = r.json()
    except Exception:
        print(f"[!] JSON解析失敗 (HTTP {r.status_code})")
        return None, None
    payload = data.get("payload", {}) or {}
    at = payload.get("accessToken") or data.get("access_token")
    if at:
        rt = payload.get("refreshToken") or data.get("refresh_token") or refresh_token
        print(f"[+] accessToken 更新OK (len={len(at)})")
        return at, rt
    # ★レスポンス全文にはトークンや個人情報が混じるのでログへ残さない
    print(f"[!] refresh に失敗しました (HTTP {r.status_code}, "
          f"code={data.get('resultInfo', {}).get('code', '?')})")
    return None, None


def get_balance(access_token, device_uuid=None):
    print(f"\n[*] アカウント情報取得...")
    headers = make_headers(device_uuid)
    session = httpx.Client(follow_redirects=False, timeout=30.0)
    hdrs = {**headers, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    info = {}
    print("  [1] getHomeDisplayInfo...")
    r = session.post(
        f"{BASE_APP4}/bff/v3/getHomeDisplayInfo",
        params={"payPayLang": "ja"},
        json={"excludeMissionBannerInfoFlag": False, "includeBeginnerFlag": False,
              "includeSkinInfoFlag": False, "networkStatus": "WIFI"},
        headers=hdrs,
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            up = payload.get("userProfile", {})
            info["name"] = up.get("displayName", "?")
            info["nick_name"] = up.get("nickName", "?")
            info["user_id"] = up.get("externalUserId", "?")
            info["internal_id"] = up.get("id", "?")
            info["phone"] = up.get("phoneNumber", "?")
            info["email"] = up.get("mailAddress", "?")
            info["paypay_id"] = up.get("payPayIdInfo", {}).get("payPayId", "?")
            info["has_wallet"] = up.get("hasWallet", False)
            info["is_premium"] = up.get("isPremium", False)
            info["kyc_status"] = up.get("kycInfo", {}).get("status", "?")
            info["signup_at"] = up.get("signUpCompletedAt", "?")
        else:
            print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")

    return info


def alive(access_token, device_uuid=None, verbose=True):
    if verbose:
        print(f"\n[*] alive() セッション維持...")
    headers = make_headers(device_uuid)
    session = httpx.Client(follow_redirects=False, timeout=30.0)
    hdrs = {**headers, "Authorization": f"Bearer {access_token}"}
    hdrs.pop("Content-Type", None)

    r1 = session.get(f"{BASE_APP4}/bff/v1/getGlobalServiceStatus", params={"payPayLang": "en"}, headers=hdrs)
    if verbose:
        print(f"  [1] getGlobalServiceStatus: {r1.status_code}")

    r2 = session.post(
        f"{BASE_APP4}/bff/v3/getHomeDisplayInfo", params={"payPayLang": "ja"},
        json={"excludeMissionBannerInfoFlag": False, "includeBeginnerFlag": False,
              "includeSkinInfoFlag": False, "networkStatus": "WIFI"},
        headers={**hdrs, "Content-Type": "application/json"},
    )
    if verbose:
        print(f"  [2] getHomeDisplayInfo: {r2.status_code}")

    r3 = session.get(f"{BASE_APP4}/bff/v1/getSearchBar", params={"payPayLang": "ja"}, headers=hdrs)
    if verbose:
        print(f"  [3] getSearchBar: {r3.status_code}")

    ok = r1.status_code == 200
    if verbose:
        print(f"  → {'OK' if ok else 'NG'}")
    return ok


# ============================================================
# PayPay API
# ============================================================

def _api_session(access_token, device_uuid=None):
    headers = make_headers(device_uuid)
    hdrs = {**headers, "Authorization": f"Bearer {access_token}"}
    return httpx.Client(follow_redirects=False, timeout=30.0), hdrs


def get_balance_info(access_token, device_uuid=None):
    """GET /bff/v1/getBalanceInfo → 残高・ポイント
    PayPaython-mobile ライブラリ準拠 (パラメータ追加 / cashBackBalanceInfo使用)"""
    print(f"\n[*] getBalanceInfo...")
    session, hdrs = _api_session(access_token, device_uuid)
    # ライブラリ準拠のパラメータ
    params = {
        "includePendingBonusLite": "false",
        "includePending": "true",
        "noCache": "true",
        "includeKycInfo": "true",
        "includePayPaySecuritiesInfo": "true",
        "includePointInvestmentInfo": "true",
        "includePayPayBankInfo": "true",
        "includeGiftVoucherInfo": "true",
        "payPayLang": "ja",
    }
    r = session.get(f"{BASE_APP4}/bff/v1/getBalanceInfo", params=params, headers=hdrs)
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        _dbg(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:800]}")
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            ws = payload.get("walletSummary", {}) or {}
            wd = payload.get("walletDetail", {}) or {}

            def _bal(v):
                """{balance: N, currency: ...} または数値から金額を取得"""
                if isinstance(v, dict):
                    return v.get("balance", 0) or 0
                if isinstance(v, (int, float)):
                    return v
                return 0

            # ライブラリ準拠: points は cashBackBalanceInfo から取得 (フォールバック: totalPayPayPointsInfo)
            points = _bal(wd.get("cashBackBalanceInfo"))
            if not points:
                points = _bal(wd.get("totalPayPayPointsInfo"))

            result = {
                "money": _bal(wd.get("emoneyBalanceInfo")),
                "money_light": _bal(wd.get("prepaidBalanceInfo")),
                "all_balance": _bal(ws.get("allTotalBalanceInfo")),
                "useable_balance": _bal(ws.get("usableBalanceInfoWithoutCashback")),
                "points": points,
            }
            print(f"    [+] parsed: {result}")
            return result
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        _dbg(f"    raw: {r.text[:500]}")
    return None


def get_payment_history(access_token, device_uuid=None, order_types=None, page_token=None, page_size=20):
    """GET /bff/v3/getPaymentHistory → 取引履歴
    PayPaython-mobile ライブラリ準拠 (必須パラメータ追加)"""
    print(f"\n[*] getPaymentHistory...")
    session, hdrs = _api_session(access_token, device_uuid)
    # ライブラリ準拠のパラメータ
    params = {
        "pageSize": str(page_size),
        "orderTypes": "",
        "paymentMethodTypes": "",
        "isOverdraftOnly": "false",
        "payPayLang": "ja",
    }
    if order_types:
        params["orderTypes"] = ",".join(order_types)
    if page_token:
        # APIが期待するパラメータ名は "nextPageKey" (レスポンスのフィールド名と同じ)
        params["nextPageKey"] = page_token
    r = session.get(f"{BASE_APP4}/bff/v3/getPaymentHistory", params=params, headers=hdrs)
    print(f"    status={r.status_code}")
    _dbg(f"    [DEBUG] params={json.dumps(params, ensure_ascii=False)}")
    try:
        data = r.json()
        _dbg(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:1200]}")
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            items = payload.get("paymentInfoList", [])
            print(f"    [+] paymentInfoList: {len(items)}件, hasNextPage: {payload.get('hasNextPage')}, nextPageKey: {payload.get('nextPageKey')}")
            # ページネーション検証: page_token指定時に同じデータが返ってきた場合の警告
            if page_token and items:
                _dbg(f"    [DEBUG] page_token使用時: 最初のorderId={items[0].get('orderId', '?')}")
            return payload
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        _dbg(f"    raw: {r.text[:500]}")
    return None


# ============================================================
# 取引履歴パース・フォーマット
# ============================================================

# 入金(受取)系のorderType
INCOMING_ORDER_TYPES = {
    "CASHBACK", "P2P_CODE_RECEPTION", "INTEREST", "P2PRECEIVE",
    "RECEIVE", "CAMPAIGN_REWARD", "POINT_CONVERSION", "GIFT",
}

# 出金(支払)系のorderType
OUTGOING_ORDER_TYPES = {
    "P2PSEND", "PAYMENT", "TOPUP", "WITHDRAW", "BANK_TRANSFER",
    "DONATION", "PURCHASE", "SEND", "TOPUP_BANK",
}

# orderType → 日本語ラベル
ORDER_TYPE_LABELS = {
    "P2PSEND": "送金",
    "P2P_CODE_RECEPTION": "QR受取",
    "CASHBACK": "キャッシュバック",
    "PAYMENT": "支払い",
    "TOPUP": "チャージ",
    "WITHDRAW": "出金",
    "BANK_TRANSFER": "銀行送金",
    "DONATION": "寄付",
    "INTEREST": "利息",
    "PURCHASE": "購入",
    "GIFT": "ギフト",
    "CAMPAIGN_REWARD": "キャンペーン",
    "POINT_CONVERSION": "ポイント交換",
}

# orderStatus → 絵文字
ORDER_STATUS_EMOJI = {
    "PENDING": "🟡",
    "COMPLETED": "✅",
    "RECEIVED": "✅",
    "REJECTED": "❌",
    "CANCELED": "🚫",
    "EXPIRED": "⏰",
    "FAILED": "⚠️",
    "SUCCESS": "✅",
    "REFUNDED": "💸",
}


def _is_incoming(order_type):
    """取引タイプが入金(受取)かどうかを判定"""
    if not order_type:
        return False
    ot = str(order_type).upper()
    if ot in INCOMING_ORDER_TYPES:
        return True
    if ot in OUTGOING_ORDER_TYPES:
        return False
    # 未知のタイプはキーワードで推測
    return any(k in ot for k in ("RECEIVE", "CASHBACK", "INTEREST", "REWARD", "GIFT"))


def _parse_datetime_jst(dt_str):
    """ISO 8601 UTC → JST短縮文字列 (例: 2026-06-30T01:09:29Z → 06/30 10:09)"""
    if not dt_str:
        return "?"
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        dt_jst = dt.astimezone(JST)
        return dt_jst.strftime("%m/%d %H:%M")
    except Exception:
        return str(dt_str)[:16]


def _format_transaction(item):
    """取引履歴1件をDiscord表示用にフォーマット"""
    desc = (item.get("description") or item.get("title") or "?")[:30]
    amount = item.get("totalAmount") or item.get("amount") or 0
    ot = item.get("orderType", "?")
    status = item.get("orderStatus", "")
    status_label = item.get("statusLabelString", "")
    dt = _parse_datetime_jst(item.get("dateTime") or item.get("createdAt"))

    # 取引番号 (orderId / subOrderId / transactionId を幅広く取得)
    order_id = item.get("orderId") or ""
    sub_order_id = item.get("subOrderId") or ""
    transaction_id = item.get("transactionId") or ""

    incoming = _is_incoming(ot)
    icon = "📥" if incoming else "📤"
    sign = "+" if incoming else "−"
    ot_label = ORDER_TYPE_LABELS.get(ot, ot)
    status_icon = ORDER_STATUS_EMOJI.get(status, "")

    # ステータス表示 (ラベル or アイコン)
    status_str = ""
    if status_label:
        status_str = f" `{status_icon}{status_label}`" if status_icon else f" `{status_label}`"
    elif status_icon:
        status_str = f" {status_icon}"

    # 取引番号表示 (短縮: 先頭12桁... で省略)
    id_str = ""
    if order_id:
        id_short = order_id[:12] + "…" if len(order_id) > 12 else order_id
        id_str = f" `#{id_short}`"

    return f"{icon} `{dt}` {sign}{yen(abs(amount))} \u2003**{desc}**\u2003_{ot_label}_{status_str}{id_str}"


def _calc_monthly_summary(items):
    """取引アイテムリストから月別収支サマリーを計算
    戻り値: {YYYY-MM: {"income": int, "expense": int, "count": int}}"""
    summary = {}
    for item in items:
        dt_str = item.get("dateTime") or item.get("createdAt")
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
            dt_jst = dt.astimezone(JST)
            month_key = dt_jst.strftime("%Y-%m")
        except Exception:
            continue

        amount = item.get("totalAmount") or item.get("amount") or 0
        ot = item.get("orderType", "")
        incoming = _is_incoming(ot)

        if month_key not in summary:
            summary[month_key] = {"income": 0, "expense": 0, "count": 0}

        if incoming:
            summary[month_key]["income"] += abs(amount)
        else:
            summary[month_key]["expense"] += abs(amount)
        summary[month_key]["count"] += 1

    return summary


def _build_history_embed(payload, order_types=None, page=0, all_items=None):
    """getPaymentHistoryのpayloadからEmbedを構築
    all_items: 全ページ集計用アイテムリスト (指定時は月間サマリーに使用、未指定時は現在ページのみ)"""
    history = payload.get("paymentInfoList", []) or payload.get("history", [])
    if not history:
        return make_embed("📜 取引履歴", description="🍃 取引履歴なし", color=COLOR["history"])

    # 月間サマリー計算 (全アイテム優先、なければ現在ページ)
    items_for_summary = all_items if all_items else history
    monthly = _calc_monthly_summary(items_for_summary)

    lines = []
    for item in history[:20]:
        lines.append(_format_transaction(item))
    body = "\n".join(lines)
    if len(body) > 3200:
        body = body[:3200] + "\n..."

    page_text = f" (P.{page+1})" if page > 0 else ""
    title_text = f"📜 取引履歴 ({len(history)}件){page_text}"
    if order_types:
        title_text += f" - {','.join(order_types)}"

    embed = make_embed(title_text, description=body, color=COLOR["history"])

    # 月間サマリーを追加 (直近3ヶ月分)
    if monthly:
        sorted_months = sorted(monthly.keys(), reverse=True)[:3]
        for mk in sorted_months:
            data = monthly[mk]
            net = data["income"] - data["expense"]
            net_str = f"{'+' if net >= 0 else ''}{yen(net)}"
            embed.add_field(
                name=f"📅 {mk} ({data['count']}件)",
                value=f"📥 {yen(data['income'])} / 📤 {yen(data['expense'])}\n差引: **{net_str}**",
                inline=True
            )

    return embed


def get_p2p_link_info(access_token, device_uuid=None, verification_code=None, web_api=False):
    verification_code = _extract_verification_code(verification_code)
    print(f"\n[*] getP2PLinkInfo (code={_mask(verification_code)})...")
    if web_api:
        r = httpx.get(
            f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo",
            params={"verificationCode": verification_code},
            headers={"Accept": "application/json", "User-Agent": UA},
            timeout=30.0,
        )
    else:
        session, hdrs = _api_session(access_token, device_uuid)
        r = session.get(
            f"{BASE_APP4}/bff/v2/getP2PLinkInfo",
            params={"payPayLang": "ja", "verificationCode": verification_code},
            headers=hdrs,
        )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            pending = payload.get("pendingP2PInfo", {})
            msg = payload.get("message", {})
            # subWalletSplit を生レスポンスから再帰探索
            sub_wallet_split = deep_find(payload, "subWalletSplit")
            if not sub_wallet_split:
                sub_wallet_split = deep_find(data, "subWalletSplit")
            # 送信者を特定するための材料。本人確認は PayPay の内部ID (externalId)
            # だけで照合する (表示名は利用者が自由に変えられるので証拠にならない)。
            # 送信者は payload.sender (displayName / externalId / photoUrl) に入る。
            # 旧形式に備えて pendingP2PInfo / message.data も候補に残す。
            msg_data = msg.get("data", {}) or {}
            sender = payload.get("sender")
            if not isinstance(sender, dict):
                sender = {}
            sender_id = (deep_find(sender, "externalId", "externalUserId", "userExternalId")
                         or deep_find(pending, "senderExternalId", "externalSenderId",
                                      "senderId", "externalUserId")
                         or deep_find(msg_data, "senderExternalId",
                                      "externalSenderId", "senderId"))
            # アイコンは送信者の中だけを探す。payload 全体を漁ると
            # テーマ画像 (全員共通) を掴んで、別人が同じ鍵で並ぶことになる。
            sender_icon = (deep_find(sender, "photoUrl", "profileImageUrl", "imageUrl",
                                     "iconUrl")
                           or deep_find(pending, "senderProfileImageUrl", "senderPhotoUrl",
                                        "profileImageUrl", "photoUrl", "imageUrl", "iconUrl"))
            result = {
                "order_status": payload.get("orderStatus", "?"),
                "sender_name": (sender.get("displayName")
                                or pending.get("senderName") or "?"),
                "amount": pending.get("amount", 0),
                "has_password": pending.get("isSetPasscode", False),
                "order_id": msg_data.get("orderId", ""),
                "chat_room_id": msg.get("chatRoomId", ""),
                "message_id": msg.get("messageId", ""),
                "request_id": msg_data.get("requestId", ""),
                "sender_external_id": str(sender_id or ""),
                "sender_icon": str(sender_icon or ""),
                "sender_raw": sender or (pending if isinstance(pending, dict) else {}),
            }
            if sub_wallet_split:
                result["subWalletSplit"] = sub_wallet_split
                print(f"    subWalletSplit={sub_wallet_split}")
            else:
                _dbg(f"    [DEBUG] subWalletSplit not found in response")
                _dbg(f"    [DEBUG] payload keys={list(payload.keys())}")
                _dbg(f"    [DEBUG] pending keys={list(pending.keys())}")
                _dbg(f"    [DEBUG] msg keys={list(msg.keys())}")
            return result
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")
    return None


def create_p2p_send_money_link(access_token, device_uuid=None, amount=0, passcode=None,
                                pochibukuro=False, theme="default-sendmoney"):
    """POST /bff/v2/executeP2PSendMoneyLink → 送金リンク作成
    PayPaython-mobile ライブラリ準拠"""
    print(f"\n[*] executeP2PSendMoneyLink (amount=¥{amount:,})...")
    session, hdrs = _api_session(access_token, device_uuid)
    request_id = str(uuid4())
    body = {
        "requestId": request_id,
        "amount": amount,
        "socketConnection": "P2P",
        "theme": theme,
        "source": "sendmoney_home_sns",
    }
    if passcode:
        body["passcode"] = passcode
    if pochibukuro:
        body["theme"] = "pochibukuro"
    r = session.post(
        f"{BASE_APP4}/bff/v2/executeP2PSendMoneyLink",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        _dbg(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:800]}")
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            link = payload.get("link", "")
            # verificationCodeをリンクから抽出 (https://pay.paypay.ne.jp/xxxx → xxxx)
            vc = ""
            if link:
                vc = link.replace("https://pay.paypay.ne.jp/", "").replace("http://pay.paypay.ne.jp/", "")
            return {
                "link": link,
                "verification_code": vc,
                "chat_room_id": payload.get("chatRoomId", ""),
                "order_id": payload.get("orderId", ""),
                "request_id": request_id,
                "payload": payload,
            }
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        _dbg(f"    raw: {r.text[:500]}")
    return None


def accept_p2p_send_money_link(access_token, device_uuid=None, verification_code=None,
                                passcode=None, link_info=None):
    verification_code = _extract_verification_code(verification_code)
    print(f"\n[*] acceptP2PSendMoneyLink (code={_mask(verification_code)})...")
    if not link_info:
        link_info = get_p2p_link_info(access_token, device_uuid, verification_code)
        if not link_info:
            print("    [!] リンク情報取得失敗")
            return None
    if link_info.get("order_status") != "PENDING":
        print(f"    [!] リンクがPENDINGでない: {link_info.get('order_status')}")
        return None

    session, hdrs = _api_session(access_token, device_uuid)
    body = {
        "requestId": link_info.get("request_id", ""),
        "orderId": link_info.get("order_id", ""),
        "verificationCode": verification_code,
        "senderMessageId": link_info.get("message_id", ""),
        "senderChannelUrl": link_info.get("chat_room_id", ""),
    }
    if passcode:
        body["passcode"] = passcode
    r = session.post(
        f"{BASE_APP4}/bff/v2/acceptP2PSendMoneyLink",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            amt = payload.get("amount") or payload.get("receivedAmount", 0)
            return {"payload": payload, "amount": amt, "order_id": payload.get("orderId", "")}
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")
    return None


def reject_p2p_send_money_link(access_token, device_uuid=None, verification_code=None, link_info=None):
    verification_code = _extract_verification_code(verification_code)
    print(f"\n[*] rejectP2PSendMoneyLink (code={_mask(verification_code)})...")
    if not link_info:
        link_info = get_p2p_link_info(access_token, device_uuid, verification_code)
        if not link_info:
            print("    [!] リンク情報取得失敗")
            return None
    session, hdrs = _api_session(access_token, device_uuid)
    body = {
        "requestId": link_info.get("request_id", ""),
        "orderId": link_info.get("order_id", ""),
        "verificationCode": verification_code,
        "senderMessageId": link_info.get("message_id", ""),
        "senderChannelUrl": link_info.get("chat_room_id", ""),
    }
    r = session.post(
        f"{BASE_APP4}/bff/v2/rejectP2PSendMoneyLink",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            return data.get("payload", {})
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")
    return None


def execute_p2p_send_money(access_token, device_uuid=None, amount=0, external_receiver_id=None):
    """POST /p2p/v3/executeP2PSendMoney → 直接送金"""
    print(f"\n[*] executeP2PSendMoney (amount=¥{amount:,}, to={external_receiver_id})...")
    session, hdrs = _api_session(access_token, device_uuid)
    request_id = str(uuid4())
    body = {
        "amount": amount,
        "theme": "DEFAULT",
        "requestId": request_id,
        "externalReceiverId": external_receiver_id,
        "ackRiskError": False,
        "source": "P2P",
        "socketConnection": "P2P",
    }
    r = session.post(
        f"{BASE_APP4}/p2p/v3/executeP2PSendMoney",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            return {
                "chat_room_id": payload.get("chatRoomId", ""),
                "order_id": payload.get("orderId", ""),
                "request_id": request_id,
            }
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")
    return None


def search_p2p_user(access_token, device_uuid=None, search_term=None, page_size=20,
                    search_type="GLOBAL_SEARCH"):
    """POST /p2p/v3/searchP2PUser → ユーザー検索"""
    print(f"\n[*] searchP2PUser (term={search_term})...")
    session, hdrs = _api_session(access_token, device_uuid)
    body = {
        "searchTerm": search_term,
        "pageToken": "",
        "pageSize": page_size,
        "isIngressSendMoney": True,
        "searchTypes": search_type,
    }
    r = session.post(
        f"{BASE_APP4}/p2p/v3/searchP2PUser",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        if data.get("header", {}).get("resultCode") == "S0000":
            return data.get("payload", {})
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
    except Exception:
        _dbg(f"    raw: {r.text[:200]}")
    return None


def _normalize_paypay_link(value):
    """値を正規PayPay請求リンクURLに正規化 (QR形式)
    - 二重プレフィックス (https://pay.paypay.ne.jp/https://qr.paypay.ne.jp/xxx) を解消
    - 既にフルURL (qr/pay.paypay.ne.jp) ならコードを抽出して正規化
    - コードのみ (p2p01_xxx 等) なら https://qr.paypay.ne.jp/ を付与
    - 空文字/None は空文字を返す"""
    if not value:
        return ""
    value = str(value).strip()
    # 再帰的にプレフィックスを削除 (二重/三重プレフィックス対策)
    changed = True
    while changed:
        changed = False
        for host in (
            "https://qr.paypay.ne.jp/", "http://qr.paypay.ne.jp/",
            "https://pay.paypay.ne.jp/", "http://pay.paypay.ne.jp/",
            "https://www.paypay.ne.jp/", "http://www.paypay.ne.jp/",
            "qr.paypay.ne.jp/", "pay.paypay.ne.jp/", "www.paypay.ne.jp/",
        ):
            if value.lower().startswith(host.lower()):
                value = value[len(host):]
                changed = True
                break
    # 残りが http/https URL ならそのまま (予期せぬURLの安全策)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    # コードのみ → qr.paypay.ne.jp を付与 (請求QRの正しいホスト)
    return f"https://qr.paypay.ne.jp/{value}"


def _extract_verification_code(value):
    """PayPayリンクURLまたはコードからverificationCodeを抽出
    例: https://qr.paypay.ne.jp/p2p01_xxx → p2p01_xxx
        https://pay.paypay.ne.jp/p2p01_xxx → p2p01_xxx
        https://pay.paypay.ne.jp/https://qr.paypay.ne.jp/p2p01_xxx → p2p01_xxx (二重プレフィックス解消)
        p2p01_xxx → p2p01_xxx"""
    if not value:
        return ""
    value = str(value).strip()
    # 再帰的にプレフィックスを削除 (二重/三重プレフィックス対策)
    changed = True
    while changed:
        changed = False
        for host in (
            "https://qr.paypay.ne.jp/", "http://qr.paypay.ne.jp/",
            "https://pay.paypay.ne.jp/", "http://pay.paypay.ne.jp/",
            "https://www.paypay.ne.jp/", "http://www.paypay.ne.jp/",
            "qr.paypay.ne.jp/", "pay.paypay.ne.jp/", "www.paypay.ne.jp/",
        ):
            if value.lower().startswith(host.lower()):
                value = value[len(host):]
                changed = True
                break
    return value


def create_p2p_code(access_token, device_uuid=None, amount=0, session_id=None):
    """POST /bff/v1/createP2PCode → 請求QRコード生成
    レスポンス構造はSEND系(executeP2PSendMoneyLink)と同じで payload.link が含まれる"""
    print(f"\n[*] createP2PCode (amount=¥{amount:,})...")
    session, hdrs = _api_session(access_token, device_uuid)
    body = {
        "amount": amount,
        "sessionId": session_id or str(uuid4()),
    }
    r = session.post(
        f"{BASE_APP4}/bff/v1/createP2PCode",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        _dbg(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:800]}")
        payload = data.get("payload", {}) or {}
        result_code = data.get("header", {}).get("resultCode", "?")
        # link, url, p2pLink, p2pCode, verificationCode を幅広くチェック → 正規化
        raw_link = (
            payload.get("link")
            or payload.get("url")
            or payload.get("p2pLink")
            or payload.get("p2pCode")
            or payload.get("verificationCode")
            or ""
        )
        link = _normalize_paypay_link(raw_link)
        if link:
            print(f"    [+] link取得成功 (resultCode={result_code}): {link[:80]}...")
            payload["link"] = link
            # verificationCode / p2pCode もコードのみに正規化して格納 (二重プレフィックス防止)
            vc_only = _extract_verification_code(raw_link)
            if vc_only:
                payload["verificationCode"] = vc_only
                payload["p2pCode"] = vc_only
            return payload
        print(f"    resultCode={result_code}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
        print(f"    raw_payload={json.dumps(payload, ensure_ascii=False)[:400]}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        _dbg(f"    raw: {r.text[:500]}")
    return None

