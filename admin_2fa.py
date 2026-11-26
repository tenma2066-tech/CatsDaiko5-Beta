"""管理画面の2段階認証（スマホの認証アプリの6桁。TOTP / RFC 6238）。

パスコードが漏れても、認証アプリの入ったスマホが無ければ管理画面に入れないようにする。
- 秘密の鍵は PAYPAY_DATA_DIR/admin_2fa.json に、注文と同じ鍵（PAYPAY_ENCRYPTION_KEY）で暗号化して置く
- 使い始めるのは管理画面の「設定」から（鍵を認証アプリに登録 → 6桁を1回入れて確認できたら有効）
- スマホをなくしたとき：サーバーで admin_2fa.json を消すと、パスコードだけの状態に戻る
外部のライブラリは使わない（標準ライブラリだけ）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
import unicodedata
import urllib.parse

import paypay_gateway

STEP, DIGITS, WINDOW = 30, 6, 1          # 30秒ごとに変わる6桁。前後1つ（時計のずれ）まで認める
_lock = threading.Lock()
_last_counter = {"value": 0}             # 同じ6桁を二度は使わせない
_pending: dict = {"secret": None, "at": 0.0}   # 設定の途中の鍵（確認できるまで保存しない。10分で捨てる）
PENDING_TTL = 600


def _path() -> str:
    return os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "admin_2fa.json")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[pos:pos + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def _match(secret: str, code: str, now: float | None = None):
    """合っていれば、その6桁の番号（時刻の区切り）。合わなければ None。"""
    # 全角の数字（日本語入力のまま打ったもの）も受け付ける。ASCII の数字だけにしてから比べる（そうしないと比較で例外になる）
    code = "".join(ch for ch in unicodedata.normalize("NFKC", str(code or "")) if ch in "0123456789")
    if len(code) != DIGITS:
        return None
    base = int((now if now is not None else time.time()) // STEP)
    for counter in range(base - WINDOW, base + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return paypay_gateway._cipher().decrypt(data["secret"].encode("ascii")).decode("ascii")
    except FileNotFoundError:
        return None


def enabled() -> bool:
    """2段階認証を使っているか。ファイルがあるのに読めないときは「使っている」とみなす（鍵の不調で素通りさせない）。"""
    if not os.path.exists(_path()):
        return False
    return True


def verify(code: str, now: float | None = None) -> bool:
    """ログインのときの6桁の確認。同じ6桁の使い回しは断る。"""
    try:
        secret = _load()
    except Exception:  # noqa: BLE001
        return False
    if not secret:
        return False
    with _lock:
        counter = _match(secret, code, now)
        if counter is None or counter <= _last_counter["value"]:
            return False
        _last_counter["value"] = counter
        return True


def begin() -> dict:
    """設定を始める：新しい鍵を作り、認証アプリに登録するための情報を返す（まだ有効にはしない）。"""
    paypay_gateway._cipher()               # 暗号鍵が無ければ、ここで分かる（保存できないのに始めさせない）
    with _lock:
        if not _pending["secret"] or time.time() - _pending["at"] > PENDING_TTL:
            _pending.update(secret=base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("="), at=time.time())
        return {"secret": _pending["secret"]}


def pending(label: str, issuer: str):
    """設定の途中なら、認証アプリに渡す情報。途中でなければ None。"""
    with _lock:
        secret = _pending["secret"] if time.time() - _pending["at"] <= PENDING_TTL else None
    if not secret:
        return None
    uri = "otpauth://totp/{}?{}".format(urllib.parse.quote(f"{issuer}:{label}"),
                                        urllib.parse.urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP}))
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "uri": uri}


def confirm(code: str) -> bool:
    """設定の仕上げ：認証アプリの6桁が合ったら、鍵を保存して有効にする。"""
    with _lock:
        secret = _pending["secret"] if time.time() - _pending["at"] <= PENDING_TTL else None
        counter = _match(secret, code) if secret else None
        if counter is None:
            return False
        token = paypay_gateway._cipher().encrypt(secret.encode("ascii")).decode("ascii")
        path = _path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"secret": token, "created": time.time()}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _pending.update(secret=None, at=0.0)
        _last_counter["value"] = counter
        return True


def cancel() -> None:
    with _lock:
        _pending.update(secret=None, at=0.0)


def disable(code: str) -> bool:
    """やめる：いまの6桁が合ったときだけ。"""
    if not verify(code):
        return False
    try:
        os.remove(_path())
    except FileNotFoundError:
        pass
    return True
