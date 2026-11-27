"""Kyash 送金リンクの受け取り（paypay_gateway.py と同じ使い方ができる形）。

流れ（receive_payment）
  1. kyash.me/payments/<code> の公開ページで、受け取れる状態か・金額が合っているかを見る（受け取らない）
  2. 受け取る前に取引明細（timeline）を控える
  3. 台帳（paypay_gateway の links テーブル）に「受け取りを試した」と記録する
  4. 受け取る（PUT /v1/links/{uuid}/receive）
  5. 明細に、同じ金額の入金が新しく増えたことを確かめて「入金済み」にする

Kyash の受け取りAPIは成功しても空の 200 しか返さない。なので「入金済み」は明細で確かめられたときだけ。
確かめられなければ保留（ky_unconfirmed）にして、confirm_received（保留の自動確認・管理者の再確認）で後から拾う。
受け取りが届いたか分からないとき（通信が途中で切れた）は、自動で投げ直さない。

台帳のキーは "ky:<code>"。PayPay のリンクと混ざらない。
受取用アカウントの情報（メール・パスワード・トークン・端末ID）は PAYPAY_ENCRYPTION_KEY で暗号化して保存する。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import github_runtime_store
import kyash_api as ka
from paypay_gateway import PayPayError, _cipher, _ledger

_logger = logging.getLogger(__name__)

PREFIX = "ky:"
LINK_RE = re.compile(r"https://(?:www\.)?kyash\.me/payments/([A-Za-z0-9_-]{6,80})(?![A-Za-z0-9_-])", re.I)
CODE_RE = re.compile(r"[A-Za-z0-9_-]{6,80}\Z")
_ROOT = Path(os.environ.get("KYASH_DATA_DIR", os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "kyash"))).resolve()
_ACCOUNT_FILE = _ROOT / "account.enc"
_BLOB = "kyash/account.enc"

CLAIM_WAIT_SEC = 25          # 受け取ったあと、明細に入金が出るのを待つ時間
LOCK_WAIT_SEC = 60           # ほかの受け取りが終わるのを待つ時間（受け取りは1件ずつ）
REFRESH_BEFORE_SEC = 72 * 3600   # トークンの期限がこれより近ければ更新する
REFRESH_GAP_SEC = 3600       # 更新を連打しない
GIVE_UP_SEC = 30 * 60        # 受け取ったか分からないまま、これだけ明細に出なければ管理者確認へ

_LOCK = threading.Lock()     # 受け取り→明細の差分照合を1件ずつに絞る（同じ額の入金を取り違えない）
_ACCOUNT_LOCK = threading.Lock()
_pending_login: dict = {}
_last: dict = {"error": "", "code": "", "at": 0.0}
NEEDS_ADMIN = ("ky_login_required", "ky_verify_required")   # 管理者が入り直すまで受け取れない


# ---------------------------------------------------------------- 受取用アカウント（暗号化して保存）
def _save_account(account: dict) -> None:
    raw = _cipher().encrypt(json.dumps(account).encode())
    if github_runtime_store.enabled():
        try:
            github_runtime_store.write_blob(_BLOB, raw)
        except github_runtime_store.RuntimeStoreError as exc:
            raise PayPayError("runtime_store", str(exc)) from exc
        return
    _ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".account-", dir=_ROOT)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, _ACCOUNT_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _load_account():
    try:
        raw = github_runtime_store.read_blob(_BLOB, _ACCOUNT_FILE)
    except github_runtime_store.RuntimeStoreError as exc:
        raise PayPayError("runtime_store", str(exc)) from exc
    if not raw:
        return None
    try:
        data = json.loads(_cipher().decrypt(raw))
        if not isinstance(data, dict) or not data.get("email") or not isinstance(data.get("device"), dict):
            raise ValueError()
        return data
    except PayPayError:
        raise
    except Exception:
        raise PayPayError("account_unreadable", "保存済みのKyash認証情報を読み取れません。") from None


def _expiry(account) -> float:
    return ka.parse_time((account or {}).get("token_expiry")) or 0.0


def status() -> dict:
    """管理画面・お客さんの画面の表示用（通信しない）。"""
    account = _load_account()
    connected = bool(account and account.get("token"))
    exp = _expiry(account)
    return {"connected": connected, "ready": connected and (not exp or exp > time.time() + 60),
            "expires_at": int(exp), "user_name": (account or {}).get("user_name") or "",
            "email": (account or {}).get("email") or "", "verify_waiting": bool(_pending_login),
            "last_error": _last["error"] if time.time() - _last["at"] < 6 * 3600 else "",
            "needs_admin": connected and _last["code"] in NEEDS_ADMIN}


def _note_error(text: str, code: str = "") -> None:
    _last.update(error=str(text)[:200], code=code, at=time.time())


def _needs_admin(code: str, message: str) -> PayPayError:
    _note_error(message, code)
    return PayPayError(code, message)


def _store_login(email: str, password: str, device: dict, token: str, refresh, expiry) -> dict:
    me = {}
    try:
        me = ka.get_me(token, device) or {}
    except ka.KyashError:
        pass
    account = {"email": email, "password": password, "device": device, "token": token,
               "refresh_token": refresh or "", "token_expiry": expiry or "",
               "public_id": me.get("publicId"), "user_name": me.get("userName") or "",
               "refreshed_at": time.time()}
    _save_account(account)
    _last.update(error="", code="", at=0.0)
    return account


def _kyash_error(exc: ka.KyashError, what: str) -> PayPayError:
    if isinstance(exc, ka.KyashNetworkError):
        return PayPayError("ky_network", f"Kyashと通信できませんでした（{what}）。少し待ってからもう一度お試しください。")
    if exc.status == 429:
        return PayPayError("ky_rate_limited", "Kyashが混み合っています。しばらくしてからもう一度お試しください。")
    if exc.status >= 500 or exc.status == 408:
        return PayPayError("ky_unavailable", "Kyash側で一時的なエラーが起きています。しばらくしてからお試しください。")
    return PayPayError("ky_rejected", f"Kyashが{what}を受け付けませんでした（{exc.text()}）。")


def begin_login(email: str, password: str) -> dict:
    """① メール・パスワード。端末IDは同じメールなら使い回す（2回目以降はメールの確認が省かれることが多い）。"""
    email, password = str(email or "").strip(), str(password or "")
    if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}", email) or not password:
        raise PayPayError("ky_bad_login", "メールアドレスとパスワードを入れてください。")
    with _ACCOUNT_LOCK:
        try:
            old = _load_account()
        except PayPayError:
            old = None
        device = old["device"] if old and old.get("email") == email else ka.gen_device()
        try:
            res = ka.login_password(email, password, device)
        except ka.KyashError as exc:
            if isinstance(exc, ka.KyashNetworkError) or exc.status >= 500:
                raise _kyash_error(exc, "ログイン") from None
            raise PayPayError("ky_bad_login", "Kyashにログインできませんでした（メールアドレスかパスワードが違う可能性があります）。") from None
        if res["verified"] and res.get("token"):
            _pending_login.clear()
            _store_login(email, password, device, res["token"], res.get("refreshToken"), res.get("tokenExpiry"))
            return {"verify_required": False}
        _pending_login.clear()
        _pending_login.update(email=email, password=password, device=device, token=res.get("token"), at=time.time())
        return {"verify_required": True}


def finish_login(pasted: str) -> dict:
    """② Kyash から届いたメール（またはSMS）の確認リンクを貼ってもらい、ログインを終える。"""
    with _ACCOUNT_LOCK:
        if not _pending_login or time.time() - _pending_login.get("at", 0) > 1800:
            _pending_login.clear()
            raise PayPayError("ky_no_pending", "先に「① ログイン」をしてください（30分以内）。")
        p = dict(_pending_login)
        try:
            res = ka.login_verify_link(pasted, p.get("token"), p["device"])
        except ka.KyashError as exc:
            if isinstance(exc, ka.KyashNetworkError) or exc.status >= 500:
                raise _kyash_error(exc, "確認") from None
            raise PayPayError("ky_bad_verify", "確認リンクを受け付けませんでした。届いた最新のリンクを、そのまま貼り付けてください。") from None
        if not res.get("token"):
            raise PayPayError("ky_bad_verify", "確認リンクからログインできませんでした。もう一度「① ログイン」からお試しください。")
        _pending_login.clear()
        _store_login(p["email"], p["password"], p["device"], res["token"], res.get("refreshToken"), res.get("tokenExpiry"))
        return {"connected": True}


def logout() -> None:
    """保存済みのトークンとパスワードを消す（端末IDとメールは残す＝次も同じ端末として入れる）。"""
    with _ACCOUNT_LOCK:
        account = _load_account()
        if account:
            for k in ("token", "refresh_token", "token_expiry", "password"):
                account[k] = ""
            _save_account(account)
        _pending_login.clear()


def _refresh(account: dict) -> dict:
    """トークンを更新する。だめならパスワードで入り直す（メール確認が要るときは管理者に任せる）。呼び出し側で _ACCOUNT_LOCK を持つこと。"""
    if account.get("refresh_token"):
        try:
            data = ka._request("POST", "/v1/session/refresh", token=account.get("token"), device=account["device"],
                               json_body={"refresh_token": account["refresh_token"]}) or {}
            if data.get("token"):
                account.update(token=data["token"], refresh_token=data.get("refreshToken") or account["refresh_token"],
                               token_expiry=data.get("tokenExpiry") or account.get("token_expiry"), refreshed_at=time.time())
                _save_account(account)
                return account
        except ka.KyashNetworkError as exc:
            raise _kyash_error(exc, "接続の更新") from None
        except ka.KyashError as exc:
            if exc.status >= 500 or exc.status == 429:
                raise _kyash_error(exc, "接続の更新") from None
    if not account.get("password"):
        raise _needs_admin("ky_login_required", "現在Kyashでの決済を受け付けられません（受取側のKyash未接続）。管理者にお知らせください。")
    try:
        res = ka.login_password(account["email"], account["password"], account["device"])
    except ka.KyashError as exc:
        if isinstance(exc, ka.KyashNetworkError) or exc.status >= 500 or exc.status == 429:
            raise _kyash_error(exc, "再ログイン") from None
        raise _needs_admin("ky_login_required", "受取側のKyashに再ログインできません。管理者にお知らせください。") from None
    if not res["verified"] or not res.get("token"):
        _pending_login.clear()
        _pending_login.update(email=account["email"], password=account["password"], device=account["device"],
                              token=res.get("token"), at=time.time())
        raise _needs_admin("ky_verify_required", "受取側のKyashがメールでの確認を求めています。管理者にお知らせください。")
    return _store_login(account["email"], account["password"], account["device"], res["token"],
                        res.get("refreshToken"), res.get("tokenExpiry"))


def _account(force: bool = False) -> dict:
    """使えるトークンの入ったアカウント。期限が近ければ更新、force=True（401を受けた）なら必ず更新。"""
    with _ACCOUNT_LOCK:
        account = _load_account()
        if not account or not account.get("token"):
            if account and account.get("password"):
                return _refresh(account)
            raise PayPayError("ky_login_required", "現在Kyashでの決済を受け付けられません（受取側のKyash未接続）。管理者にお知らせください。")
        exp = _expiry(account)
        soon = exp and exp - time.time() < REFRESH_BEFORE_SEC
        if force or (soon and time.time() - float(account.get("refreshed_at") or 0) > REFRESH_GAP_SEC):
            return _refresh(account)
        return account


def _call(fn, *args):
    """fn(*args, token, device)。401 は処理されていないので、更新して1回だけ投げ直す。"""
    account = _account()
    try:
        return fn(*args, account["token"], account["device"])
    except ka.KyashNetworkError:
        raise
    except ka.KyashError as exc:
        if exc.status != 401:
            raise
    account = _account(force=True)
    return fn(*args, account["token"], account["device"])


def keepalive() -> str:
    """定期的に呼ぶ（app.py の見張り）。期限が近ければ更新し、トークンが生きているか確かめる。"""
    try:
        account = _load_account()
    except PayPayError as exc:
        _note_error(exc.message)
        return exc.code
    if not account or not (account.get("token") or account.get("password")):
        return "not_connected"
    try:
        _call(lambda token, device: ka.get_me(token, device))
        _last.update(error="", code="", at=0.0)
        return "ok"
    except PayPayError as exc:
        _note_error(exc.message, exc.code)
        return exc.code
    except ka.KyashError as exc:
        _note_error(exc.text())
        return "error"


async def refresh_session() -> dict:
    """管理画面の「Kyashの接続を更新」。"""
    account = _account(force=True)
    return {"expires_at": int(_expiry(account))}


# ---------------------------------------------------------------- リンク
def normalize_link(raw):
    """kyash.me/payments/<code> → "ky:<code>"。それ以外は None。"""
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    text = raw.strip()
    if text.startswith(PREFIX) and CODE_RE.fullmatch(text[len(PREFIX):]):
        return text
    codes = LINK_RE.findall(text)
    if not codes or len(set(codes)) != 1:
        return None
    return PREFIX + codes[0]


def looks_like(raw) -> bool:
    return isinstance(raw, str) and bool(re.search(r"kyash\.me/", raw, re.I))


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _inspect(code: str, amount: int) -> dict:
    """公開ページを見る（受け取らない）。受け取れる状態で金額が合っていれば {uuid, sender...}。"""
    try:
        r = ka.resolve_kyash_link(code[len(PREFIX):])
    except ka.KyashError as exc:
        raise _kyash_error(exc, "リンクの確認") from None
    if r.get("http_status") and int(r["http_status"]) == 404:
        raise PayPayError("link_not_found", "このKyashリンクは見つかりません。リンクが間違っているか、取り消されています。")
    if r.get("http_status") and int(r["http_status"]) >= 500:
        raise PayPayError("ky_unavailable", "Kyash側で一時的なエラーが起きています。しばらくしてからお試しください。")
    if r.get("http_status") and int(r["http_status"]) >= 400:
        raise PayPayError("link_check", f"Kyashリンクを確認できませんでした（応答 {r['http_status']}）。")
    if not r.get("uuid"):
        raise PayPayError("link_cancelled", "このKyashリンクは受け取れません（受け取り済み・取り消し・期限切れ）。新しい送金リンクを作ってください。")
    if not ka.looks_like_uuid(r["uuid"]):
        raise PayPayError("link_incomplete", "Kyashリンクの情報を読み取れませんでした。")
    if not isinstance(r.get("amount"), int) or r["amount"] <= 0:
        raise PayPayError("link_incomplete", "送金リンクの金額を確認できません。")
    if r["amount"] != amount:
        raise PayPayError("amount_mismatch", f"金額が違います（リンク {r['amount']:,}円／必要 {amount:,}円）。{amount:,}円ちょうどの新しいリンクを作ってください。")
    return r


def _incoming(items, amount: int) -> list:
    """明細のうち、この金額の入金（transfer・プラスの額）。新しい順のまま。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or len(it) != 1:
            continue
        kind = next(iter(it))
        sub = it.get(kind)
        if kind != "transfer" or not isinstance(sub, dict):
            continue
        amt = sub.get("amount")
        tx = sub.get("txUUIDAPI")
        if type(amt) is not int or amt != int(amount) or not isinstance(tx, str) or not tx:
            continue
        out.append({"tx": tx, "at": ka.parse_time(sub.get("createdAt"))})
    return out


def _tx_ids(items) -> list:
    out = []
    for it in items or []:
        if isinstance(it, dict) and len(it) == 1:
            sub = it.get(next(iter(it)))
            if isinstance(sub, dict) and isinstance(sub.get("txUUIDAPI"), str):
                out.append(sub["txUUIDAPI"])
    return out


def _used_tx(db, except_digest: str) -> set:
    """ほかの注文の入金としてすでに使った明細（同じ入金を2つの注文に充てない）。"""
    used = set()
    for row in db.execute("SELECT extra FROM links WHERE provider='kyash' AND digest<>? AND created>?",
                          (except_digest, time.time() - 30 * 86400)):
        try:
            tx = json.loads(row["extra"] or "{}").get("tx")
        except ValueError:
            continue
        if tx:
            used.add(tx)
    return used


def _extra(row) -> dict:
    try:
        data = json.loads(row["extra"] or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, IndexError, KeyError):
        return {}


def _set(digest: str, **fields) -> None:
    extra = fields.pop("extra", None)
    with _ledger() as db:
        if extra is not None:
            fields["extra"] = json.dumps(extra)[:4000]
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE links SET {cols} WHERE digest=?", (*fields.values(), digest))


def _find_new_incoming(digest: str, amount: int, seen: set, since: float, wait: float):
    """明細に、この金額の新しい入金が出るのを待つ。見つかれば tx を返す。"""
    deadline = time.time() + wait
    while True:
        try:
            items = _call(lambda token, device: ka.get_timeline(50, token, device))
        except (ka.KyashError, PayPayError):
            items = None
        if items is not None:
            with _ledger() as db:
                used = _used_tx(db, digest)
            for c in reversed(_incoming(items, amount)):   # 古いものから（同じ額が続いたときに、先に受け取った注文へ先に充てる）
                if c["tx"] in seen or c["tx"] in used:
                    continue
                if c["at"] and c["at"] < since - 120:   # 受け取りより前の入金は、このリンクの分ではない
                    continue
                return c["tx"]
        if time.time() >= deadline:
            return None
        time.sleep(2.0)


def _claim(digest: str, uuid: str, amount: int, seen: set, extra: dict, fresh: bool) -> str:
    """受け取って、明細で確かめる。呼び出し側で _LOCK を持つこと。
    fresh=True: 初めての受け取り。受け取っていないと確実なら台帳から消し、同じリンクでやり直せるようにする。
    fresh=False: 保留の再確認から。台帳は消さない（注文とリンクのつながりを残す）。"""
    claimed_at = time.time()
    extra.update(uuid=uuid, claimed_at=claimed_at, seen=sorted(seen)[-60:])
    _set(digest, state="attempted", extra=extra, last_status="claim:sending")
    try:
        _call(lambda token, device: ka.claim_link(uuid, token, device))
    except ka.KyashNetworkError as exc:
        if not exc.maybe_sent:
            if fresh:   # 届いていない → 台帳から消す（同じリンクでやり直せる）
                with _ledger() as db:
                    db.execute("DELETE FROM links WHERE digest=? AND state='attempted'", (digest,))
            else:
                _set(digest, state="review", last_status="claim:not_sent")
            raise PayPayError("ky_network", "Kyashと通信できませんでした。少し待ってからもう一度お試しください（送金はそのままで大丈夫です）。") from None
        _set(digest, state="review", last_status="claim:uncertain")
        raise PayPayError("ky_unconfirmed", "受け取りの結果がまだ分かりません。自動で確認を続けます（同じリンクを送り直したり、再送金したりしないでください）。") from None
    except ka.KyashError as exc:
        if exc.status >= 500 or exc.status == 408:
            _set(digest, state="review", last_status=f"claim:{exc.status}")
            raise PayPayError("ky_unconfirmed", "受け取りの結果がまだ分かりません。自動で確認を続けます（同じリンクを送り直したり、再送金したりしないでください）。") from None
        if not fresh:
            _set(digest, state="review", last_status=f"claim:rejected:{exc.status}")
            raise PayPayError("hold_failed", f"Kyashで受け取れませんでした（{exc.text()}）。管理者に連絡してください。") from None
        with _ledger() as db:   # はっきり断られた（受け取っていない）→ 台帳から消す
            db.execute("DELETE FROM links WHERE digest=? AND state='attempted'", (digest,))
        raise PayPayError("ky_claim_rejected", f"Kyashで受け取れませんでした（{exc.text()}）。送金リンクはそのまま残っています。管理者にお知らせください。") from None
    except PayPayError:
        if fresh:   # 受け取りを送る前に止まった（接続切れ等）
            with _ledger() as db:
                db.execute("DELETE FROM links WHERE digest=? AND state='attempted'", (digest,))
        else:
            _set(digest, state="review")
        raise
    _set(digest, state="review", last_status="claim:200")
    tx = _find_new_incoming(digest, amount, seen, claimed_at, CLAIM_WAIT_SEC)
    if not tx:
        raise PayPayError("ky_unconfirmed", "受け取りましたが、Kyashの明細への反映を待っています。自動で確認を続けます（再送金はしないでください）。")
    extra["tx"] = tx
    _set(digest, state="paid", extra=extra, last_status="claim:200/明細で確認")
    return digest


def _acquire():
    if not _LOCK.acquire(timeout=LOCK_WAIT_SEC):
        raise PayPayError("ky_busy", "Kyashの受け取りが混み合っています。少し待ってからもう一度お試しください。")


async def receive_payment(raw_link: str, amount: int, purchase_id: str) -> str:
    code = normalize_link(raw_link)
    if not code:
        raise PayPayError("bad_link", "Kyashの送金リンク（https://kyash.me/payments/…）の形式が正しくありません。リンクだけを貼り付けてください。")
    digest = _digest(code)
    with _ledger() as db:
        known = db.execute("SELECT order_id FROM links WHERE digest=?", (digest,)).fetchone()
    if known:
        if known["order_id"] == purchase_id:
            return await confirm_received(code, purchase_id, amount)
        raise PayPayError("duplicate", "このリンクは以前の注文で使われています。")
    info = _inspect(code, amount)
    _acquire()
    try:
        account = _account()
        if info.get("sender_user_name") and account.get("user_name") and info["sender_user_name"] == account["user_name"]:
            raise PayPayError("identity", "受取側と同じアカウントのリンクは使えません。")
        try:
            before = _call(lambda token, device: ka.get_timeline(50, token, device))
        except ka.KyashError as exc:
            raise _kyash_error(exc, "明細の確認") from None
        with _ledger() as db:
            try:
                db.execute("INSERT INTO links(digest,order_id,state,created,receiver,amount,sender_id,sender_name,provider) VALUES(?,?,?,?,?,?,?,?,?)",
                           (digest, purchase_id, "attempted", time.time(), str(account.get("public_id") or ""), amount,
                            str(info.get("sender_user_name") or "")[:80], str(info.get("sender_name") or "")[:40], "kyash"))
            except sqlite3.IntegrityError:
                raise PayPayError("duplicate", "このリンクまたは注文はすでに使用済みです。新しい送金リンクを作ってください。") from None
        return _claim(digest, info["uuid"], amount, set(_tx_ids(before)), {}, True)
    finally:
        _LOCK.release()


async def confirm_received(code: str, purchase_id: str, amount: int | None = None) -> str:
    """保留（結果不明）になった Kyash の注文の再確認。
    - 明細に入金が見つかる → 支払い済み
    - リンクがまだ受け取れる状態（受け取りが届いていなかった）→ 金額を確かめてもう一度受け取る
    - どちらでもない → しばらくは待つ。GIVE_UP_SEC を過ぎたら管理者確認へ（hold_failed）"""
    if not isinstance(code, str) or not code.startswith(PREFIX) or not CODE_RE.fullmatch(code[len(PREFIX):]):
        raise PayPayError("not_found", "確認できる送金リンクがありません。")
    digest = _digest(code)
    with _ledger() as db:
        row = db.execute("SELECT order_id, state, amount, extra, created FROM links WHERE digest=?", (digest,)).fetchone()
    if not row or row["order_id"] != purchase_id:
        raise PayPayError("not_found", "この注文の送金記録が見つかりません。")
    if row["state"] == "paid":
        return digest
    if row["state"] not in ("pending", "review", "attempted"):
        raise PayPayError("not_found", "この送金は確認できない状態です。")
    want = int(row["amount"] if row["amount"] is not None else (amount or 0))
    if want <= 0:
        raise PayPayError("still_pending", "まだ受け取りが完了していません。")
    extra = _extra(row)
    _acquire()
    try:
        seen = set(extra.get("seen") or [])
        since = float(extra.get("claimed_at") or row["created"] or time.time())
        tx = _find_new_incoming(digest, want, seen, since, 0)
        if tx:
            extra["tx"] = tx
            _set(digest, state="paid", extra=extra, last_status="recheck/明細で確認")
            return digest
        try:
            r = ka.resolve_kyash_link(code[len(PREFIX):])
        except ka.KyashError as exc:
            raise _kyash_error(exc, "リンクの確認") from None
        if r.get("uuid") and ka.looks_like_uuid(r["uuid"]) and r.get("amount") == want:
            # まだ受け取れる状態 ＝ 前回の受け取りは届いていなかった → もう一度受け取る
            try:
                before = _call(lambda token, device: ka.get_timeline(50, token, device))
            except ka.KyashError as exc:
                raise _kyash_error(exc, "明細の確認") from None
            return _claim(digest, r["uuid"], want, seen | set(_tx_ids(before)), extra, False)
        _set(digest, last_status=f"recheck/page:{r.get('http_status')}/{'uuid' if r.get('uuid') else 'none'}")
        if time.time() - since > GIVE_UP_SEC:
            _set(digest, state="review")
            raise PayPayError("hold_failed", "Kyashの明細で入金を確認できませんでした。管理者に連絡してください（送金済みかどうかはKyashアプリの取引履歴で確認できます）。")
        raise PayPayError("ky_unconfirmed", "Kyashの明細への反映を待っています。自動で確認を続けます（再送金はしないでください）。")
    finally:
        _LOCK.release()
