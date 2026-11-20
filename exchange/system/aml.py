#!/usr/bin/env python3
"""
aml.py -- マネロン対策: Discord垢 ↔ PayPay垢 / Kyash垢 の紐付け (事前登録) と毎回の名義照合

■ 事前登録 (初回だけ・サービスごとに1回)
  1. BOTが「¥1 (既定) の送金リンクを、制限時間内に作って貼って」と指定する
  2. 利用者は自分のPayPay / Kyashで送金リンクを作ってBOTに貼る
  3. BOTはリンクの送信者 (PayPay=内部ID externalId / Kyash=publicId) を
     そのDiscordアカウントに紐付ける
       - PayPay: 受け取る前にリンクの中身で送信者が分かる。¥1は受け取って消費する
                 (受け取らない設定にもできる。その場合は本人がキャンセルする)
       - Kyash : 送信者は受け取った後の明細にしか出ない。受け取ってから特定する

■ 登録は初回だけ。以後は換金のたびに、受け取る**前**に送信者を照合する
  登録だけでは「1回目は自分、2回目から他人の金」を止められない。
  他人名義の入金が通る経路を残すと、詐欺の被害金や名義貸しの受け皿にされる。
  登録と違う垢から来た送金は**受け取らずに換金を拒否**する (設定で変えられない)。
    PayPay → Kyash : リンク情報の送信者ID (externalId) で照合
    Kyash → PayPay : リンクの公開ページに出る送信者の Kyash ID で照合
                     (受け取った後に明細の publicId でもう一度確かめる。
                      ID の付け替えですり抜けた分だけは、受け取ってしまって
                      いるので送信者へそのまま返す)
  送信者を読み取れないリンクも、照合できないので受け取らない。
  払い出しも登録済みの垢にしか出さない (第三者への横流しを止める)。

■ 識別材料
  PayPay は内部ID (externalId)、Kyash は publicId (数値・不変) を正とする。
  Kyash は受取前に publicId が見えないので、受取前は Kyash ID (一意) で照合し、
  受取後に publicId で裏を取る。表示名は利用者が自由に変えられるので証拠に
  しない (改名の検知と管理者向けの表示のためだけに記録している)。

■ 1つの垢 = 1つのDiscordアカウント
  既に別のDiscordアカウントに紐付いている垢では登録できない。
  複数のDiscordアカウントで上限を回避する経路と、他人の垢を横取りする経路を塞ぐ。
"""
from __future__ import annotations

import re
import secrets as _secrets
import sys
import threading
import time
import unicodedata

import store

for _s in (sys.stdout, sys.stderr):   # cp932コンソール対策
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

SERVICES = store.SERVICES
LABEL = store.SERVICE_LABEL

# 設定に出さない安全弁 (いくら設定を間違えても、これ以上は外部APIを叩かせない)
RETRY_COOLDOWN_SEC = 5         # 貼り直しの最短間隔
ISSUE_COOLDOWN_SEC = 3         # 金額の出し直しの最短間隔
HOURLY_ATTEMPT_CAP = 20        # 1ユーザーが1時間に投げられる照合の回数
HOURLY_ISSUE_CAP = 30          # 1ユーザーが1時間に金額を出せる回数
AMOUNT_HISTORY_SEC = 3600      # ランダム指定の時に「前に出した額」を避ける期間


# ============================================================
# 設定の読み出し (壊れた値でも動く範囲に丸める)
# ============================================================

def _int(cfg: dict, key: str, lo: int, hi: int) -> int:
    try:
        v = int((cfg or {}).get(key, store.DEFAULT_CONFIG[key]))
    except (TypeError, ValueError):
        v = int(store.DEFAULT_CONFIG[key])
    return max(lo, min(hi, v))


def window_sec(cfg: dict) -> int:
    return _int(cfg, "reg_window_sec", 30, 1800)


def max_attempts(cfg: dict) -> int:
    return _int(cfg, "reg_max_attempts", 1, 10)


def amount_range(cfg: dict) -> tuple[int, int]:
    """登録リンクの金額。既定は ¥1 固定。上限を大きくしない (間違えても痛くない額に)。"""
    lo = _int(cfg, "reg_amount", 1, 100)
    hi = _int(cfg, "reg_amount_max", 1, 100)
    return (lo, max(lo, hi))


# ============================================================
# 送信者の識別材料
# ============================================================

def normalize_name(name) -> str:
    """全角/半角・空白の違いで別人扱いにしない (改名の検知用。照合には使わない)"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    return re.sub(r"\s+", "", s).casefold()


def paypay_identity(info: dict | None) -> dict:
    """PayPayのリンク情報 (paypay_api.get_p2p_link_info) → 識別材料"""
    info = info or {}
    name = str(info.get("sender_name") or "").strip()
    if name in ("?", "-", "??"):
        name = ""
    return {"service": "paypay",
            "id": str(info.get("sender_external_id") or "").strip(),
            "name": name,
            "icon": str(info.get("sender_icon") or "").strip()}


def kyash_identity(sender: dict | None) -> dict:
    """Kyashの受取結果 (kyash_bridge.claim_and_identify の sender) → 識別材料"""
    sender = sender or {}
    pid = sender.get("public_id")
    return {"service": "kyash",
            "id": str(pid).strip() if pid not in (None, "") else "",
            "name": str(sender.get("name_full") or "").strip(),
            "user_name": str(sender.get("user_name") or "").strip(),
            "icon": str(sender.get("image_url") or "").strip()}


def kyash_page_identity(link: dict | None) -> dict:
    """Kyashリンクの公開ページ (kyash_bridge.inspect_link) → 受取前の識別材料。

    publicId はページに出ないので id は空。照合は Kyash ID (user_name) で行う。
    """
    link = link or {}
    return {"service": "kyash", "id": "",
            "name": str(link.get("sender_name") or "").strip(),
            "user_name": str(link.get("sender_user_name") or "").strip(),
            "icon": str(link.get("sender_icon") or "").strip()}


def same_kyash_id(a, b) -> bool:
    """Kyash ID の比較 (大文字小文字・前後の @ と空白は無視する)"""
    na = normalize_name(str(a or "").strip().lstrip("@"))
    nb = normalize_name(str(b or "").strip().lstrip("@"))
    return bool(na) and na == nb


def display(identity_or_binding: dict | None) -> str:
    """埋め込み表示用の1行"""
    d = identity_or_binding or {}
    parts = []
    if d.get("name"):
        parts.append(str(d["name"]))
    if d.get("user_name"):
        parts.append(f"@{d['user_name']}")
    if not parts:
        parts.append("(名前を取得できず)")
    if d.get("id"):
        parts.append(f"ID `{str(d['id'])[:24]}`")
    return " / ".join(parts)


def renamed(binding: dict | None, identity: dict) -> tuple[str, str] | None:
    """IDは同じで名前が変わったか (同一人物の改名として記録する)"""
    b = binding or {}
    old = b.get("user_name") or b.get("name") or ""
    new = identity.get("user_name") or identity.get("name") or ""
    if old and new and normalize_name(old) != normalize_name(new):
        return (old, new)
    return None


# ============================================================
# チャレンジ (指定額 + 制限時間)
#
# 寿命が数分のものをファイルに書く意味は無いのでメモリに持つ。
# BOTを再起動したら消えるが、それは「やり直し」であって事故ではない。
# ============================================================

_lock = threading.RLock()
_challenges: dict[tuple[int, str], dict] = {}
_attempts: dict[int, list[float]] = {}
_issues: dict[int, list[float]] = {}
_issued_amounts: dict[tuple[int, str], list[tuple[int, float]]] = {}


def _recent(log: dict, uid: int, span: float) -> list[float]:
    now = time.time()
    hits = [t for t in log.get(uid, []) if now - t < span]
    log[uid] = hits
    return hits


def _prune() -> None:
    now = time.time()
    for k in [k for k, c in _challenges.items() if now - float(c.get("issued_at", 0)) > 3600]:
        _challenges.pop(k, None)
    for log in (_attempts, _issues):
        for uid in list(log):
            if not _recent(log, uid, 3600):
                log.pop(uid, None)
    for k in list(_issued_amounts):
        left = [(a, t) for a, t in _issued_amounts[k] if now - t < AMOUNT_HISTORY_SEC]
        if left:
            _issued_amounts[k] = left
        else:
            _issued_amounts.pop(k, None)


def _pick_amount(cfg: dict, key: tuple[int, str]) -> int:
    """固定額ならそれ。範囲指定なら直近に出した額を避けて選ぶ。

    出し直しで同じ額が出ると、時間切れになった前回のリンクをそのまま
    貼れてしまう = 「その場で作った」証明が弱くなる。
    """
    lo, hi = amount_range(cfg)
    if lo == hi:
        return lo
    pool = list(range(lo, hi + 1))
    now = time.time()
    recent = {a for a, t in _issued_amounts.get(key, []) if now - t < AMOUNT_HISTORY_SEC}
    left = [a for a in pool if a not in recent] or pool
    return left[_secrets.randbelow(len(left))]


def issue_challenge(cfg: dict, user_id: int, service: str) -> dict:
    """金額を決めて時計を回す。

    戻り: {"ok":True, "challenge":{...}} / {"ok":False,"reason":..,"wait":秒}
    """
    uid = int(user_id)
    key = (uid, service)
    now = time.time()
    with _lock:
        _prune()
        issues = _recent(_issues, uid, 3600)
        if issues and now - issues[-1] < ISSUE_COOLDOWN_SEC:
            return {"ok": False, "reason": "COOLDOWN",
                    "wait": ISSUE_COOLDOWN_SEC - (now - issues[-1])}
        if len(issues) >= HOURLY_ISSUE_CAP:
            return {"ok": False, "reason": "RATE_LIMIT", "wait": 3600 - (now - issues[0])}
        amount = _pick_amount(cfg, key)
        ch = {"user_id": uid, "service": service, "amount": amount, "issued_at": now,
              "expires_at": now + window_sec(cfg), "attempts": 0, "last_attempt_at": 0.0}
        _challenges[key] = ch
        issues.append(now)
        _issues[uid] = issues
        _issued_amounts.setdefault(key, []).append((amount, now))
        return {"ok": True, "challenge": dict(ch)}


def get_challenge(user_id: int, service: str) -> dict | None:
    with _lock:
        ch = _challenges.get((int(user_id), service))
        if not ch or time.time() > float(ch["expires_at"]):
            return None
        return dict(ch)


def drop_challenge(user_id: int, service: str) -> None:
    with _lock:
        _challenges.pop((int(user_id), service), None)


def take_attempt(cfg: dict, user_id: int, service: str) -> dict:
    """照合を1回消費する。**外部APIを叩く前に**必ず通すこと。

    期限の判定もここでやる。API往復の後に判定すると、通信が遅れただけで
    「間に合ったのに時間切れ」になる。

    戻り: {"ok":True,"challenge":{...}} /
          {"ok":False,"reason":"NO_CHALLENGE|EXPIRED|TOO_MANY|COOLDOWN|RATE_LIMIT","wait":秒}
    """
    uid = int(user_id)
    key = (uid, service)
    now = time.time()
    with _lock:
        ch = _challenges.get(key)
        if not ch:
            return {"ok": False, "reason": "NO_CHALLENGE"}
        if now > float(ch["expires_at"]):
            _challenges.pop(key, None)
            return {"ok": False, "reason": "EXPIRED"}
        if int(ch["attempts"]) >= max_attempts(cfg):
            return {"ok": False, "reason": "TOO_MANY"}
        last = float(ch.get("last_attempt_at") or 0)
        if last and now - last < RETRY_COOLDOWN_SEC:
            return {"ok": False, "reason": "COOLDOWN", "wait": RETRY_COOLDOWN_SEC - (now - last)}
        hits = _recent(_attempts, uid, 3600)
        if len(hits) >= HOURLY_ATTEMPT_CAP:
            return {"ok": False, "reason": "RATE_LIMIT", "wait": 3600 - (now - hits[0])}
        hits.append(now)
        _attempts[uid] = hits
        ch["attempts"] = int(ch["attempts"]) + 1
        ch["last_attempt_at"] = now
        return {"ok": True, "challenge": dict(ch)}


def attempts_left(cfg: dict, user_id: int, service: str) -> int:
    ch = get_challenge(user_id, service)
    if not ch:
        return 0
    return max(0, max_attempts(cfg) - int(ch.get("attempts", 0)))


# ============================================================
# 判定
# ============================================================

def needs(direction: str) -> tuple[str, str]:
    """(受け取る側のサービス, 払い出す側のサービス)"""
    return ("paypay", "kyash") if direction == "p2k" else ("kyash", "paypay")


def gate(user_id: int) -> dict:
    """リンクを見る前の入口判定。

    換金はどちら向きでも**両方の登録**を要求する。受け取る側は送信者の照合に、
    払い出す側は送り先の固定に使う (払い出しを登録済みの垢にしか出さない)。

    戻り: {"state": "ok"|"blocked"|"register", "missing": [service, ...]}
    """
    acc = store.get_account(user_id) or {}
    if acc.get("state") == "BLOCKED":
        return {"state": "blocked", "missing": []}
    missing = [s for s in SERVICES if not store.binding(user_id, s)]
    return {"state": "register" if missing else "ok", "missing": missing}


def judge_registration(cfg: dict, user_id: int, service: str, identity: dict,
                       *, amount: int, want: int, ref: str = "") -> dict:
    """このリンクで登録してよいか。

    戻り: {"ok":True} / {"ok":False, "reason":..., "message":"利用者に見せる文", ...}
    PayPayは受け取る前、Kyashは受け取った後に呼ぶ (Kyashは受け取らないと送信者が分からない)。
    """
    uid = int(user_id)
    label = LABEL[service]
    if int(amount) != int(want):
        return {"ok": False, "reason": "AMOUNT",
                "message": (f"リンクの金額が **¥{int(amount):,}** になっている。\n"
                            f"**¥{int(want):,} ちょうど**のリンクを作り直して貼って。")}
    if not identity.get("id"):
        # ここで通すと「誰とも照合できない紐付け」が出来て、以後の照合が全部素通りになる
        return {"ok": False, "reason": "NO_IDENTITY",
                "message": (f"{label}側から送信者のIDを取得できなかった。\n"
                            "少し待ってからもう一度試して。続くなら管理者に連絡して。")}

    acc = store.get_account(uid) or {}
    if acc.get("state") == "BLOCKED":
        return {"ok": False, "reason": "BLOCKED",
                "message": "このアカウントは管理者によって利用停止されている。"}
    if store.is_blocked_identity(service, identity["id"]):
        return {"ok": False, "reason": "BLOCKED_ID",
                "message": (f"この{label}アカウントは利用停止の対象になっている。\n"
                            "心当たりが無ければ管理者に連絡して。")}

    owner = store.find_owner(service, identity["id"], exclude_user_id=uid)
    if owner:
        return {"ok": False, "reason": "DUPLICATE", "owner": owner,
                "message": (f"この{label}アカウントは既に**別のDiscordアカウント**に登録されている。\n"
                            f"1つの{label}アカウントで複数のDiscordアカウントは登録できない。\n"
                            "心当たりが無ければ管理者に連絡して。")}

    if ref and store.find_by_ref(service, ref, exclude_user_id=uid):
        return {"ok": False, "reason": "LINK_REUSED",
                "message": "このリンクは既に別の登録で使われている。新しく作り直して。"}

    cur = store.binding(uid, service)
    if cur and str(cur.get("id")) != identity["id"] and not cfg.get("reg_allow_rebind", False):
        return {"ok": False, "reason": "REBIND",
                "message": (f"あなたには既に別の{label}アカウント ({display(cur)}) が登録されている。\n"
                            "付け替えは管理者が `/本人確認解除` してからでないとできない。")}
    return {"ok": True, "reason": ""}


def precheck_kyash_registration(user_id: int, page: dict) -> dict:
    """Kyash登録リンクを受け取る**前**の足切り (公開ページの Kyash ID で見る)。

    publicId は受け取らないと分からないので、ここで止められるのは
    「明らかに他人の垢 / 利用停止の垢」だけ。本当の判定は受取後の judge_registration。
    """
    uname = page.get("user_name") or ""
    if not uname:
        return {"ok": True}
    owner = store.find_kyash_owner_by_user_name(uname, exclude_user_id=int(user_id))
    if owner:
        return {"ok": False, "reason": "DUPLICATE", "owner": owner,
                "message": (f"このKyashアカウント (@{uname}) は既に**別のDiscordアカウント**に"
                            "登録されている。\n心当たりが無ければ管理者に連絡して。\n"
                            "-# 受け取っていないので、リンクは取り消せば戻る。")}
    if store.is_blocked_kyash_user_name(uname):
        return {"ok": False, "reason": "BLOCKED_ID",
                "message": ("このKyashアカウントは利用停止の対象になっている。\n"
                            "-# 受け取っていないので、リンクは取り消せば戻る。")}
    return {"ok": True}


def _gate_common(user_id: int, service: str) -> dict | None:
    acc = store.get_account(int(user_id)) or {}
    if acc.get("state") == "BLOCKED":
        return {"action": "deny", "reason": "BLOCKED"}
    if not store.binding(int(user_id), service):
        return {"action": "register", "reason": "UNREGISTERED"}
    return None


def check_paypay_sender(user_id: int, identity: dict) -> dict:
    """PayPay→Kyash。受け取る**前**に呼ぶ。

    戻り: {"action": "allow"|"deny"|"register", "reason": ..., ...}
    登録と違う垢・別人の垢・送信者IDが読めないリンクは、どれも受け取らずに断る。
    """
    uid = int(user_id)
    g = _gate_common(uid, "paypay")
    if g:
        return g
    cur = store.binding(uid, "paypay")
    if not identity.get("id"):
        return {"action": "deny", "reason": "NO_IDENTITY", "binding": cur}
    if str(cur.get("id")) == identity["id"]:
        return {"action": "allow", "reason": "MATCH", "binding": cur,
                "renamed": renamed(cur, identity)}
    owner = store.find_owner("paypay", identity["id"], exclude_user_id=uid)
    return {"action": "deny", "reason": "OTHER_OWNER" if owner else "MISMATCH",
            "binding": cur, "owner": owner}


def check_kyash_page(user_id: int, page: dict) -> dict:
    """Kyash→PayPay。受け取る**前**に、公開ページの Kyash ID で照合する。

    戻り: {"action": "allow"|"deny"|"register", "reason": ..., ...}
    """
    uid = int(user_id)
    g = _gate_common(uid, "kyash")
    if g:
        return g
    cur = store.binding(uid, "kyash")
    uname = page.get("user_name") or ""
    if not uname:
        return {"action": "deny", "reason": "NO_IDENTITY", "binding": cur}
    if same_kyash_id(cur.get("user_name"), uname):
        return {"action": "allow", "reason": "MATCH", "binding": cur}
    owner = store.find_kyash_owner_by_user_name(uname, exclude_user_id=uid)
    return {"action": "deny", "reason": "OTHER_OWNER" if owner else "MISMATCH",
            "binding": cur, "owner": owner}


def check_kyash_received(user_id: int, identity: dict) -> dict:
    """Kyash→PayPay。受け取った**後**に、明細の publicId で裏を取る。

    戻り: {"action": "allow"|"return"|"hold", "reason": ..., ...}
      allow   登録済みの垢 (publicId 一致) から来ていた
      return  受取前の Kyash ID は一致したのに publicId が違った (ID の付け替え等)。
              受け取ってしまっているので、送信者へそのまま返す
      hold    明細から送信者を特定できなかった (管理者が確認する)
    """
    uid = int(user_id)
    cur = store.binding(uid, "kyash")
    if not identity.get("id"):
        return {"action": "hold", "reason": "NO_IDENTITY", "binding": cur}
    if cur and str(cur.get("id")) == identity["id"]:
        acc = store.get_account(uid) or {}
        if acc.get("state") == "BLOCKED":
            return {"action": "return", "reason": "BLOCKED", "binding": cur}
        return {"action": "allow", "reason": "MATCH", "binding": cur,
                "renamed": renamed(cur, identity)}
    owner = store.find_owner("kyash", identity["id"], exclude_user_id=uid)
    return {"action": "return", "reason": "OTHER_OWNER" if owner else "MISMATCH",
            "binding": cur, "owner": owner}


REASON_JP = {
    "MATCH": "登録済みの垢と一致",
    "MISMATCH": "登録と違う垢から送られた",
    "OTHER_OWNER": "別のDiscordアカウントに登録済みの垢",
    "NO_IDENTITY": "送信者のIDを取得できない",
    "BLOCKED": "利用停止中のアカウント",
    "BLOCKED_ID": "利用停止の対象になっている垢",
    "UNREGISTERED": "本人確認が未完了",
    "AMOUNT": "指定額と違う",
    "DUPLICATE": "既に別のDiscordアカウントに紐付いている",
    "LINK_REUSED": "他の登録で使われたリンク",
    "REBIND": "別の垢が登録済み (付け替え不可)",
    "NO_CHALLENGE": "登録の受付が始まっていない",
    "EXPIRED": "制限時間切れ",
    "TOO_MANY": "貼り直しの回数切れ",
    "COOLDOWN": "連投",
    "RATE_LIMIT": "試行回数が多すぎる",
}


def reason_text(reason: str) -> str:
    return REASON_JP.get(str(reason or ""), str(reason or "?"))


if __name__ == "__main__":
    cfg = dict(store.DEFAULT_CONFIG)
    print("登録金額:", amount_range(cfg), "/ 制限", window_sec(cfg), "秒")
    pp = paypay_identity({"sender_name": "山田 太郎", "sender_external_id": "ext-1"})
    ky = kyash_identity({"public_id": 12345, "user_name": "taro", "name_full": "山田太郎"})
    print("PayPay:", display(pp))
    print("Kyash :", display(ky))
    print("改名  :", renamed({"user_name": "taro"}, {"user_name": "taro2"}))
