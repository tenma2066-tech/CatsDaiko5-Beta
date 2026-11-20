#!/usr/bin/env python3
"""
kyash_bridge.py -- kyash_api を BOT から使うための手順まとめ

■ リンクの検査 (受け取らない)
  inspect_link() は kyash.me の公開ページだけを見る。金額・受取用UUID・
  送信者の Kyash ID が分かる。受け取る前の照合 (aml.check_kyash_page) はこれで行う。

■ 受け取りと送信者の特定
  Kyash の受取API (PUT /v1/links/{uuid}/receive) は空の 200 しか返さず、
  送信者の publicId は受け取った後の明細 (timeline) にしか出ない。
  受取前に明細を控えておき、受取後に「新しく増えた入金」を探して送信者を確定する。
    - 時計のずれに左右されない (時刻ではなく差分で探す)
    - 同じ額の入金がたまたま同時に来て、送信者が食い違えば「特定できない」にする
      (取り違えて他人を本人扱いするよりは、管理者に回す方がまし)

■ 払い出し
  送金 (POST /v1/transactions) が通信の途中で切れた時は「失敗」にしない。
  サーバーに届いていれば送金は成立しているので、投げ直すと二重送金になる。
  "unknown" として返し、呼び出し側は UNCERTAIN で止めて明細で確かめる。
"""
from __future__ import annotations

import re
import threading
import time

import kyash_api as ka

KyashError = ka.KyashError
KyashNetworkError = ka.KyashNetworkError

_claim_lock = threading.Lock()      # 受取→明細の差分照合を1件ずつに絞る
_send_lock = threading.Lock()       # 払い出しを1件ずつ (残高の取り合いをしない)
_refresh_lock = threading.Lock()
_last_refresh = 0.0
REFRESH_COOLDOWN_SEC = 300

_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?kyash\.me/payments/([A-Za-z0-9_\-]{6,80})")


# ============================================================
# 共通
# ============================================================

def ready() -> bool:
    return ka.is_ready()


def account() -> dict:
    """ログイン中のBOT側アカウント (トークンは返さない)"""
    acc = ka.get_account() or {}
    return {"email": acc.get("email") or ka.get_active_email() or "",
            "public_id": acc.get("publicId"), "user_name": acc.get("userName") or "",
            "remaining_h": (ka.token_remaining_seconds() or 0) / 3600 if acc.get("token") else 0}


def _refresh_after_401() -> bool:
    """401 の後だけ呼ぶ。冷却付き (連打で失効させない)"""
    global _last_refresh
    with _refresh_lock:
        if time.time() - _last_refresh < REFRESH_COOLDOWN_SEC:
            return False
        _last_refresh = time.time()
        try:
            ka.refresh_session()
            return True
        except KyashError as e:
            print(f"[kyash] トークン更新に失敗: {e.text()}")
            return False


def _call(fn, *a, **kw):
    """401 なら1回だけトークンを更新して投げ直す (401 は処理されていないので安全)"""
    try:
        return fn(*a, **kw)
    except KyashNetworkError:
        raise
    except KyashError as e:
        if e.status == 401 and _refresh_after_401():
            return fn(*a, **kw)
        raise


def balance() -> int | None:
    try:
        b = _call(ka.get_balance) or {}
        v = b.get("amount")
        return int(v) if v is not None else None
    except KyashError as e:
        print(f"[kyash] 残高取得に失敗: {e.text()}")
        return None


def balance_detail() -> dict | None:
    try:
        return _call(ka.get_balance)
    except KyashError:
        return None


def alive() -> bool:
    return ka.alive_check()


def keepalive() -> tuple[bool, str]:
    """常駐から呼ぶ。期限が近ければ更新する。戻り: (使える状態か, 説明)"""
    if not ka.is_ready():
        return False, "未ログイン"
    try:
        changed, rem_h, action = ka.ensure_fresh(min_hours=72, min_gap_hours=24)
    except KyashError as e:
        return False, f"更新に失敗: {e.text()}"
    if action == "need_login":
        return False, "トークンが失効していて更新もできない"
    if not ka.alive_check():
        return False, "疎通確認に失敗"
    return True, (f"{action} / 残り{rem_h:.0f}時間" if rem_h is not None else action)


# ============================================================
# リンクの検査 (受け取らない)
# ============================================================

def extract_code(raw: str) -> str:
    """kyash.me/payments/<code> のURL → code。URL以外は受け付けない (空文字)。"""
    s = str(raw or "").strip()
    m = _LINK_RE.search(s)
    return m.group(1) if m else ""


def inspect_link(raw: str) -> dict:
    """公開ページを見るだけ。

    戻り: {"ok": bool, "reason", "error", "code", "uuid", "amount",
           "sender_name", "sender_user_name", "sender_icon"}
    ok=True は「今から受け取れる状態で、金額が読めた」こと。
    """
    code = extract_code(raw)
    base = {"ok": False, "reason": "", "error": "", "code": code, "uuid": "",
            "amount": 0, "sender_name": "", "sender_user_name": "", "sender_icon": ""}
    if not code:
        return {**base, "reason": "BAD_LINK",
                "error": "Kyashの送金リンク (https://kyash.me/payments/…) を貼って"}
    try:
        r = ka.resolve_kyash_link(code)
    except KyashError as e:
        return {**base, "reason": "NETWORK", "error": e.text()}
    out = {**base, **{k: r.get(k) or base[k] for k in
                      ("uuid", "sender_name", "sender_user_name", "sender_icon")}}
    out["amount"] = int(r.get("amount") or 0)
    if r.get("http_status") and int(r["http_status"]) >= 400:
        return {**out, "reason": "BAD_LINK",
                "error": f"リンクを開けない (HTTP {r['http_status']})。URLを確認して"}
    if not out["uuid"]:
        return {**out, "reason": "NOT_PENDING",
                "error": "このリンクは受け取れない (受取済み・取消済み・期限切れ)"}
    if out["amount"] <= 0:
        return {**out, "reason": "NO_AMOUNT", "error": "リンクの金額を読み取れない"}
    return {**out, "ok": True}


# ============================================================
# 受け取り + 送信者の特定
# ============================================================

def _counterparty(sub: dict, own_id) -> dict:
    """明細1件の相手方。入金の transfer では target に送信者が入る (実測)。"""
    for side in ("target", "source", "user"):
        p = sub.get(side) or {}
        if isinstance(p, dict) and p.get("publicId") not in (None, ""):
            if own_id is not None and str(p.get("publicId")) == str(own_id):
                continue
            return p
    return {}


def _incoming(items: list, amount: int) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not it:
            continue
        kind = next(iter(it))
        sub = it.get(kind) or {}
        amt = sub.get("amount")
        # 受け取ったリンクは送信者付きの transfer (+額) として載る (実測)。
        # 自分の送金リンクの払い戻し等が紛れないよう transfer だけを見る
        if kind != "transfer":
            continue
        if not isinstance(amt, int) or amt <= 0 or amt != int(amount):
            continue
        out.append({"kind": kind, "sub": sub, "tx": str(sub.get("txUUIDAPI") or ""),
                    "created_at": sub.get("createdAt")})
    return out


def _sender_of(cands: list[dict], own_id) -> dict | None:
    """候補から送信者を1人に決める。食い違えば None (特定できない)。"""
    parties = {}
    for c in cands:
        p = _counterparty(c["sub"], own_id)
        if not p:
            return None
        parties[str(p.get("publicId"))] = (p, c)
    if len(parties) != 1:
        return None
    p, c = next(iter(parties.values()))
    return {"public_id": p.get("publicId"), "user_name": p.get("userName") or "",
            "name_full": p.get("nameFull") or "", "image_url": p.get("imageUrl") or "",
            "tx": c["tx"], "created_at": c["created_at"],
            "breakdown": c["sub"].get("amountBreakdown") or {}}


def _tx_ids(items: list) -> set[str]:
    out = set()
    for it in items or []:
        if isinstance(it, dict) and it:
            sub = it.get(next(iter(it))) or {}
            if sub.get("txUUIDAPI"):
                out.add(str(sub["txUUIDAPI"]))
    return out


def claim_and_identify(uuid: str, amount: int, exclude_tx: set[str] | None = None,
                       wait_sec: float = 25.0) -> dict:
    """リンクを受け取り、明細の差分から送信者を特定する。

    戻り値の status:
      received      受け取れた。sender に {public_id, user_name, name_full, tx, breakdown}
      unidentified  受け取れたが送信者を特定できない (反映遅れ・同額の同時入金)
      unknown       受け取れたか分からない (通信が途中で切れた)
      failed        受け取れていない (reason: NO_TOKEN / REJECTED / NETWORK / SNAPSHOT)
    """
    exclude_tx = set(exclude_tx or ())
    own = (ka.get_account() or {}).get("publicId")
    with _claim_lock:
        try:
            before = _call(ka.get_timeline, 40)
        except KyashNetworkError as e:
            return {"status": "failed", "reason": "SNAPSHOT", "error": e.text()}
        except KyashError as e:
            # 明細を控えられないと、受け取っても送信者を確かめられない。受け取らない
            reason = "NO_TOKEN" if e.status in (0, 401) else "SNAPSHOT"
            return {"status": "failed", "reason": reason, "error": e.text()}
        seen = _tx_ids(before) | exclude_tx

        claimed_at = time.time()
        maybe = False
        try:
            _call(ka.claim_link, uuid)
        except KyashNetworkError as e:
            if not e.maybe_sent:
                return {"status": "failed", "reason": "NETWORK", "error": e.text()}
            maybe = True
        except KyashError as e:
            if e.status >= 500 or e.status == 408:
                return {"status": "unknown", "claimed_at": claimed_at, "error": e.text()}
            reason = "NO_TOKEN" if e.status in (0, 401) else "REJECTED"
            return {"status": "failed", "reason": reason, "error": e.text(),
                    "http": e.status}

        deadline = time.time() + wait_sec
        cands: list[dict] = []
        while True:
            try:
                after = _call(ka.get_timeline, 40)
                cands = [c for c in _incoming(after, amount) if c["tx"] not in seen]
            except KyashError:
                cands = []
            if cands or time.time() >= deadline:
                break
            time.sleep(2.0)

    base = {"claimed_at": claimed_at, "amount": int(amount), "uuid": uuid}
    if not cands:
        if maybe:
            return {**base, "status": "unknown",
                    "error": "受取APIの応答が途切れ、明細にも入金が見当たらない"}
        return {**base, "status": "unidentified",
                "error": "受け取れたが、明細に入金がまだ出ていない"}
    sender = _sender_of(cands, own)
    if not sender:
        return {**base, "status": "unidentified", "candidates": len(cands),
                "error": "同じ額の入金が同時に複数あり、送信者を1人に決められない"}
    return {**base, "status": "received", "sender": sender}


def identify_later(amount: int, claimed_at: float, exclude_tx: set[str],
                   window_before: float = 120, window_after: float = 1800) -> dict:
    """受け取った後に送信者を特定できなかった分を、後から明細で探し直す。

    差分を取れないので時刻で絞る。候補が1人に決まらなければ特定しない。
    """
    own = (ka.get_account() or {}).get("publicId")
    try:
        items = _call(ka.get_timeline, 50)
    except KyashError as e:
        return {"status": "error", "error": e.text()}
    lo, hi = claimed_at - window_before, claimed_at + window_after
    cands = []
    for c in _incoming(items, amount):
        if c["tx"] in exclude_tx:
            continue
        ts = ka.parse_time(c["created_at"])
        if ts is None or not (lo <= ts <= hi):
            continue
        cands.append(c)
    if not cands:
        return {"status": "absent"}
    sender = _sender_of(cands, own)
    if not sender:
        return {"status": "ambiguous", "candidates": len(cands)}
    return {"status": "received", "sender": sender}


# ============================================================
# 払い出し / 返金
# ============================================================

def send(amount: int, public_id, user_name: str, *, request_id=None) -> dict:
    """登録済みのKyash垢 (または返金先) へ直接送る。

    戻り: {"status": "sent"|"failed"|"unknown", "group", "tx", "error"}
    """
    with _send_lock:
        try:
            r = _call(ka.send_money, int(amount), public_id, user_name, request_id=request_id)
        except KyashNetworkError as e:
            return {"status": "unknown" if e.maybe_sent else "failed", "error": e.text()}
        except KyashError as e:
            return {"status": "unknown" if e.status >= 500 or e.status == 408 else "failed",
                    "error": e.text(), "http": e.status}
    if not (r.get("group") or r.get("tx")):
        # 2xx なのに取引IDが無い。送れているかもしれないので失敗扱いにしない
        return {"status": "unknown", "error": "送金APIの応答に取引IDが無い", **r}
    return {"status": "sent", **r}


def send_link(amount: int, *, request_id=None) -> dict:
    """送金リンクを作る。戻り: {"status": "sent"|"failed"|"unknown", "url", "link_id"}"""
    with _send_lock:
        try:
            url, data = _call(ka.create_send_link, int(amount), request_id=request_id)
        except KyashNetworkError as e:
            return {"status": "unknown" if e.maybe_sent else "failed", "error": e.text()}
        except KyashError as e:
            return {"status": "unknown" if e.status >= 500 or e.status == 408 else "failed",
                    "error": e.text(), "http": e.status}
    if not url:
        return {"status": "unknown", "error": "リンク作成APIの応答にURLが無い"}
    link_id = str((data or {}).get("uuid") or (data or {}).get("uuidAPI")
                  or (data or {}).get("id") or "")
    return {"status": "sent", "url": url, "link_id": link_id}


def find_outgoing(amount: int, public_id, request_id: str) -> dict | None:
    """送信前に保存した取引 UUID を返す明細だけを注文に結び付ける。"""
    if not request_id:
        return None
    try:
        items = _call(ka.get_timeline, 50)
    except KyashError:
        return None
    match = None
    for it in items or []:
        if not isinstance(it, dict) or not it:
            continue
        kind = next(iter(it))
        sub = it.get(kind) or {}
        if kind != "transfer" or sub.get("amount") != -int(amount):
            continue
        tgt = sub.get("target") or {}
        if str(tgt.get("publicId")) != str(public_id):
            continue
        if sub.get("uuidClient") == request_id and sub.get("txUUIDAPI"):
            if match and match["tx"] != sub["txUUIDAPI"]:
                return None
            match = {"tx": sub["txUUIDAPI"], "created_at": sub.get("createdAt")}
    return match


# ============================================================
# ログイン (Discord の /kyashログイン と CLI の kyash_login.py が同じこれを呼ぶ)
# ============================================================

LOGIN_STATE_TTL = 900


def login_step1(email: str, password: str) -> dict:
    """戻り: {"status": "ok"|"link_required"|"error", ...}"""
    email = str(email or "").strip()
    if "@" not in email or not password:
        return {"status": "error", "error": "メールアドレスとパスワードを入れて"}
    try:
        res = ka.login_password(email, password)
    except KyashError as e:
        return {"status": "error", "error": f"ログイン失敗: {e.text()}"}
    finally:
        password = ""
    if res.get("verified") and res.get("token"):
        ka.finalize_login(email, res["token"], res.get("refreshToken"),
                          res.get("tokenExpiry"), res["device"])
        return {"status": "ok", "email": email, **account()}
    return {"status": "link_required", "email": email, "device": res["device"],
            "provisional": res.get("token"), "expires_at": time.time() + LOGIN_STATE_TTL}


def login_step2(state: dict, pasted: str) -> dict:
    if not state or state.get("status") != "link_required":
        return {"status": "error", "error": "ログインの途中経過が無い"}
    if float(state.get("expires_at", 0)) < time.time():
        return {"status": "error", "error": "時間切れ。最初からやり直して"}
    try:
        r = ka.login_verify_link(pasted, state.get("provisional"), state["device"])
    except KyashError as e:
        return {"status": "error", "error": f"リンク検証に失敗: {e.text()}"}
    if not r.get("token"):
        return {"status": "error", "error": "検証は通ったがトークンが返らなかった"}
    ka.finalize_login(state["email"], r["token"], r.get("refreshToken"),
                      r.get("tokenExpiry"), state["device"])
    return {"status": "ok", "email": state["email"], **account()}


def logout() -> list[str]:
    return ka.remove_tokens()
