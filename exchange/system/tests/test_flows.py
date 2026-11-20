#!/usr/bin/env python3
"""
外部API (PayPay / Kyash / Discord) を全部モックにして、換金の主要な流れを通しで確かめる。

    py system\\tests\\test_flows.py

実際のお金は一切動かない。データは一時フォルダに書く (本番の json には触らない)。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import store  # noqa: E402

TMP = tempfile.mkdtemp(prefix="pk_test_")
for name in ("CONFIG_FILE", "ORDERS_FILE", "ACCOUNTS_FILE", "USED_FILE", "HOLDS_FILE", "DETECT_FILE"):
    setattr(store, name, os.path.join(TMP, os.path.basename(getattr(store, name))))

import aml  # noqa: E402
import bot as B  # noqa: E402
import kyash_bridge  # noqa: E402
import kyash_api  # noqa: E402

REAL_CLAIM = kyash_bridge.claim_and_identify     # モックに差し替える前の本物

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label} {detail}")


# ------------------------------------------------------------
# Discord のモック
# ------------------------------------------------------------

class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.mention = f"<@{uid}>"

    def __str__(self):
        return self.name


def _check_view(kw):
    # 本物の discord.py と同じく、view に None 等を渡したら落とす
    if "view" in kw and not hasattr(kw["view"], "__discord_ui_view__"):
        raise TypeError(f"view must be a View, not {type(kw['view']).__name__}")


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        _check_view(kw)
        self.log.append({"content": content, **kw})


class FakeResponse:
    def __init__(self, log):
        self.log = log
        self._done = False

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, **kw):
        _check_view(kw)
        self._done = True
        self.log.append({"content": content, **kw})

    async def send_modal(self, modal):
        self._done = True
        self.log.append({"modal": modal})

    def is_done(self):
        return self._done


class FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.log = []
        self.followup = FakeFollowup(self.log)
        self.response = FakeResponse(self.log)
        self.guild_id = 1
        self.channel = None


def titles(inter):
    out = []
    for m in inter.log:
        e = m.get("embed")
        if e is not None:
            out.append(e.title)
        elif m.get("content"):
            out.append(m["content"])
    return out


DMS = []


async def fake_dm(uid, **kw):
    DMS.append((uid, kw))
    return True


async def fake_notify(text, kind="error"):
    print(f"     [通知] {text.splitlines()[0][:90]}")


B._dm = fake_dm
B.notify_admin = fake_notify


async def fake_log_event(title, description, **kw):
    print(f"     [ログ] {title}")


B.log_event = fake_log_event


async def fake_log_trade(o):
    pass


B.log_trade = fake_log_trade

# ------------------------------------------------------------
# PayPay のモック
# ------------------------------------------------------------

PP_LINKS = {}       # code → info
PP_CALLS = {"receive": [], "payout_link": [], "payout_direct": []}
PP_STATE = {"balance": 100000, "receive_status": "received"}


def pp_link(code, amount, ext_id, name="山田", status="PENDING", money=None):
    PP_LINKS[code] = {"order_status": status, "sender_name": name, "amount": amount,
                      "has_password": False, "order_id": f"OID-{code}", "chat_room_id": "c",
                      "message_id": "m", "request_id": "r", "sender_external_id": ext_id,
                      "sender_icon": "", "code": code,
                      "subWalletSplit": {"senderEmoneyAmount": amount if money is None else money,
                                         "senderPrepaidAmount": 0 if money is None else amount - money}}
    return f"https://pay.paypay.ne.jp/{code}"


def pp_extract(v):
    return str(v).rsplit("/", 1)[-1]


def pp_inspect(link):
    code = pp_extract(link)
    info = PP_LINKS.get(code)
    if not info:
        return {"ok": False, "reason": "BAD_LINK", "error": "無効", "info": None}
    info = dict(info)
    if B.paypay.classify_status(info["order_status"]) != "try":
        return {"ok": False, "reason": "NOT_PENDING", "error": "受取不可", "info": info}
    return {"ok": True, "reason": "", "error": "", "info": info}


def pp_receive(link, passcode=None, info=None, history_pages=5):
    code = pp_extract(link)
    PP_CALLS["receive"].append(code)
    st = PP_STATE["receive_status"]
    info = PP_LINKS[code]
    if st == "received":
        info["order_status"] = "COMPLETED"
        return {"status": "received", "ok": True, "amount": info["amount"],
                "order_id": info["order_id"], "code": code}
    if st == "held":
        return {"status": "held", "ok": False, "result_code": "42007013", "code": code}
    return {"status": "failed", "ok": False, "reason": "ACCEPT_FAILED", "error": "だめ"}


def pp_payout_link(amount, passcode=None):
    PP_CALLS["payout_link"].append(amount)
    return {"status": PP_STATE.get("payout_status", "sent"), "link": "https://pay.paypay.ne.jp/OUT",
            "order_id": "PAYOUT1", "error": "x"}


B.paypay.inspect_link = pp_inspect
B.paypay.receive_link = pp_receive
B.paypay.extract_code = pp_extract
B.paypay.payout_link = pp_payout_link
B.paypay.payout_direct = lambda a, e: (PP_CALLS["payout_direct"].append((a, e)) or
                                       {"status": "sent", "order_id": "D1"})
B.paypay.sendable_balance = lambda: PP_STATE["balance"]
B.paypay.account_info = lambda: {"access_token": "t", "phone": "09000000000"}
B.paypay.find_incoming = lambda oid, pages=5: ("found", {"orderId": oid, "amount": 3000})

# ------------------------------------------------------------
# Kyash のモック (bridge の高レベル関数)
# ------------------------------------------------------------

KY_LINKS = {}       # code → page
KY_CALLS = {"claim": [], "send": [], "send_link": []}
KY_STATE = {"balance": 100000, "claim": {}}


def ky_link(code, amount, sender_uname, sender_pid, name="タロウ"):
    code = f"PcvSV{code}"          # 実物は28文字前後
    uuid = f"00000000-0000-4000-8000-{abs(hash(code)) % 10**12:012d}"
    KY_LINKS[code] = {"ok": True, "reason": "", "error": "", "code": code, "uuid": uuid,
                      "amount": amount, "sender_name": name, "sender_user_name": sender_uname,
                      "sender_icon": ""}
    KY_STATE["claim"][uuid] = {"public_id": sender_pid, "user_name": sender_uname,
                               "name_full": name, "tx": f"TX-{code}", "breakdown": {}}
    return f"https://kyash.me/payments/{code}"


def ky_inspect(raw):
    code = kyash_bridge.extract_code(raw)
    p = KY_LINKS.get(code)
    if not p:
        return {"ok": False, "reason": "BAD_LINK", "error": "無効"}
    return dict(p)


def ky_claim(uuid, amount, exclude_tx=None, wait_sec=25.0):
    KY_CALLS["claim"].append(uuid)
    mode = KY_STATE.get("claim_mode", "received")
    if mode == "unidentified":
        return {"status": "unidentified", "claimed_at": time.time() - 120, "amount": amount}
    return {"status": "received", "sender": dict(KY_STATE["claim"][uuid]),
            "claimed_at": time.time(), "amount": amount}


def ky_send(amount, pid, uname, *, request_id=None):
    KY_CALLS["send"].append((amount, str(pid), uname))
    return {"status": KY_STATE.get("send_status", "sent"), "group": "G", "tx": "T", "error": "x"}


B.kyash.ready = lambda: True
B.kyash.inspect_link = ky_inspect
B.kyash.claim_and_identify = ky_claim
B.kyash.send = ky_send
B.kyash.send_link = lambda a, *, request_id=None: (KY_CALLS["send_link"].append(a) or
                               {"status": "sent", "url": "https://kyash.me/payments/OUT"})
B.kyash.balance = lambda: KY_STATE["balance"]


# ------------------------------------------------------------
# シナリオ
# ------------------------------------------------------------

ALICE = FakeUser(1001, "alice")
BOB = FakeUser(1002, "bob")
MALLORY = FakeUser(1003, "mallory")


async def register(user, svc, link, passcode=None):
    cfg = store.load_config()
    r = aml.issue_challenge(cfg, user.id, svc)
    assert r["ok"], r
    aml._issues.clear()        # 出し直しのクールダウンを飛ばす
    it = FakeInteraction(user)
    if svc == "paypay":
        await B.process_reg_paypay(it, link, passcode)
    else:
        await B.process_reg_kyash(it, link)
    for k in list(aml._challenges):
        c = aml._challenges[k]
        c["last_attempt_at"] = 0
    aml._attempts.clear()
    return it


async def main():
    for svc in store.SERVICES:
        B.REG_SUBMIT_VIEWS[svc] = B.RegSubmitView(svc)

    print("\n[1] 本人確認 (初回だけ) — ¥1 の送金リンクで紐付け")
    it = await register(ALICE, "paypay", pp_link("regA", 1, "ext-alice", "アリス"))
    check("PayPay登録できた", store.binding(ALICE.id, "paypay") is not None, titles(it))
    check("¥1 リンクを受け取った", "regA" in PP_CALLS["receive"])
    it = await register(ALICE, "kyash", ky_link("kyregA", 1, "alice_k", 5551))
    b = store.binding(ALICE.id, "kyash")
    check("Kyash登録できた (publicId)", b and b["id"] == "5551" and b["user_name"] == "alice_k",
          titles(it))
    check("両方そろって換金できる状態", aml.gate(ALICE.id)["state"] == "ok")

    print("\n[2] 1つの垢は1人まで — 他人のPayPay垢で登録できない")
    it = await register(MALLORY, "paypay", pp_link("regM", 1, "ext-alice", "アリス"))
    check("重複登録を拒否", store.binding(MALLORY.id, "paypay") is None, titles(it))
    check("受け取っていない", "regM" not in PP_CALLS["receive"])
    it = await register(MALLORY, "kyash", ky_link("kyregM", 1, "alice_k", 5551))
    check("Kyashも受取前に重複を拒否", not any(u == KY_LINKS["PcvSVkyregM"]["uuid"] for u in KY_CALLS["claim"]),
          titles(it))

    print("\n[3] 金額違いの登録リンク")
    it = await register(BOB, "paypay", pp_link("regB_bad", 5, "ext-bob", "ボブ"))
    check("¥5 のリンクは登録に使えない", store.binding(BOB.id, "paypay") is None)
    check("受け取っていない", "regB_bad" not in PP_CALLS["receive"])
    await register(BOB, "paypay", pp_link("regB", 1, "ext-bob", "ボブ"))
    await register(BOB, "kyash", ky_link("kyregB", 1, "bob_k", 7772))
    check("BOBも登録完了", aml.gate(BOB.id)["state"] == "ok")

    print("\n[4] PayPay → Kyash (本人の垢から)")
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k1", 3000, "ext-alice"), None)
    o = store.user_orders(ALICE.id, 1)[0]
    check("注文 SENT", o["status"] == "SENT", (o["status"], o.get("error"), titles(it)))
    check("手数料5% → ¥2,850 を登録済みKyash(5551)へ", KY_CALLS["send"][-1] == (2850, "5551", "alice_k"),
          KY_CALLS["send"])

    print("\n[5] PayPay → Kyash (他人のPayPay垢から) → 受け取らずに拒否")
    n = len(PP_CALLS["receive"])
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k_other", 3000, "ext-stranger", "知らない人"), None)
    check("受取APIを呼んでいない", len(PP_CALLS["receive"]) == n)
    check("拒否の表示", any("拒否" in (t or "") for t in titles(it)), titles(it))
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k_bob", 3000, "ext-bob", "ボブ"), None)
    check("BOBの垢からALICEが換金 → 受け取らずに拒否", len(PP_CALLS["receive"]) == n)
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k_noid", 3000, "", "?"), None)
    check("送信者IDが読めない → 受け取らずに拒否", len(PP_CALLS["receive"]) == n, titles(it))

    print("\n[6] Kyash → PayPay (本人の垢から)")
    it = FakeInteraction(ALICE)
    await B.process_k2p(it, ky_link("k2p1", 2000, "alice_k", 5551))
    o = store.user_orders(ALICE.id, 1)[0]
    check("注文 SENT", o["status"] == "SENT", (o["status"], o.get("hold_reason"), titles(it)))
    check("¥1,900 のPayPay送金リンクを作った", PP_CALLS["payout_link"][-1] == 1900)
    check("リンクをDMにも控えた", any(uid == ALICE.id for uid, _ in DMS))

    print("\n[7] Kyash → PayPay (他人のKyash IDから) → 受け取らずに拒否")
    n = len(KY_CALLS["claim"])
    it = FakeInteraction(ALICE)
    await B.process_k2p(it, ky_link("k2p_other", 2000, "stranger_k", 9999))
    check("受取APIを呼んでいない", len(KY_CALLS["claim"]) == n)
    check("拒否の表示", any("拒否" in (t or "") for t in titles(it)), titles(it))
    it = FakeInteraction(ALICE)
    await B.process_k2p(it, ky_link("k2p_noid", 2000, "", 9999))
    check("ページからIDが読めない → 受け取らずに拒否", len(KY_CALLS["claim"]) == n)

    print("\n[8] Kyash → PayPay (ページのIDは本人なのに publicId が違う = 付け替え) → 送信者へ返金")
    it = FakeInteraction(ALICE)
    url = ky_link("k2p_swap", 2000, "alice_k", 8888)
    await B.process_k2p(it, url)
    o = store.user_orders(ALICE.id, 1)[0]
    check("注文 RETURNED", o["status"] == "RETURNED", (o["status"], o.get("hold_reason")))
    check("全額 ¥2,000 を実際の送信者(8888)へ返した", KY_CALLS["send"][-1][:2] == (2000, "8888"))
    check("PayPayは払い出していない", PP_CALLS["payout_link"][-1] == 1900 and len(PP_CALLS["payout_link"]) == 1)

    print("\n[9] Kyash → PayPay (送信者の特定が遅れた) → 後追いで特定して払い出し")
    KY_STATE["claim_mode"] = "unidentified"
    it = FakeInteraction(BOB)
    url = ky_link("k2p_late", 1500, "bob_k", 7772)
    await B.process_k2p(it, url)
    KY_STATE["claim_mode"] = "received"
    o = store.user_orders(BOB.id, 1)[0]
    check("まず HOLD (UNIDENTIFIED)", o["status"] == "HOLD" and o["hold_code"] == "UNIDENTIFIED",
          (o["status"], o.get("hold_code")))
    B.kyash.identify_later = lambda amount, claimed, exclude: {
        "status": "received", "sender": dict(KY_STATE["claim"][KY_LINKS["PcvSVk2p_late"]["uuid"]])}
    await B._reidentify_once()
    o = store.get_order(o["id"])
    check("後追いで SENT", o["status"] == "SENT", (o["status"], o.get("hold_reason")))

    print("\n[10] 払い出し側の残高不足 → 受け取らずに断る")
    KY_STATE["balance"] = 100
    B.invalidate_balance("kyash")
    n = len(PP_CALLS["receive"])
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k_nofund", 3000, "ext-alice"), None)
    check("受取APIを呼んでいない", len(PP_CALLS["receive"]) == n, titles(it))
    KY_STATE["balance"] = 100000
    B.invalidate_balance("kyash")

    print("\n[11] 同じリンクの使い回し")
    url = pp_link("p2k_dup", 1000, "ext-alice")
    await B.process_p2k(FakeInteraction(ALICE), url, None)
    PP_LINKS["p2k_dup"]["order_status"] = "PENDING"      # 仮に状態を戻されても
    n = len(PP_CALLS["receive"])
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, url, None)
    check("2回目は受け取らない", len(PP_CALLS["receive"]) == n, titles(it))

    print("\n[12] PayPay の一時保留 → 承認後に入金履歴で裏取りして払い出し")
    PP_STATE["receive_status"] = "held"
    it = FakeInteraction(BOB)
    url = pp_link("p2k_hold", 3000, "ext-bob")
    await B.process_p2k(it, url, None)
    hold = store.get_hold(store.paypay_link_key(PP_LINKS["p2k_hold"]))
    check("WATCHING になった", hold and hold["state"] == "WATCHING", hold and hold.get("state"))
    PP_LINKS["p2k_hold"]["order_status"] = "COMPLETED"   # 送信者が承認して自動で着金
    r = await B.guarded_check_hold(hold, force=True, reply=B.reply_dm(BOB.id))
    o = store.user_orders(BOB.id, 1)[0]
    check("承認を検知して SENT", r == "AUTO_RECEIVED" and o["status"] == "SENT", (r, o["status"]))
    PP_STATE["receive_status"] = "received"

    print("\n[13] 払い出しの成否不明 → UNCERTAIN (投げ直さない)")
    KY_STATE["send_status"] = "unknown"
    before = len(KY_CALLS["send"])
    await B.process_p2k(FakeInteraction(ALICE), pp_link("p2k_unk", 2000, "ext-alice"), None)
    o = store.user_orders(ALICE.id, 1)[0]
    check("UNCERTAIN", o["status"] == "UNCERTAIN", o["status"])
    check("送金は1回だけ", len(KY_CALLS["send"]) == before + 1)
    KY_STATE["send_status"] = "sent"
    r = await B.payout(o["id"])
    check("UNCERTAIN は /再送 経路でも払わない", r.get("status") == "busy", r)

    print("\n[14] 利用停止")
    store.set_user_state(MALLORY.id, "BLOCKED", 1, "test")
    check("gate=blocked", aml.gate(MALLORY.id)["state"] == "blocked")
    store.set_user_state(BOB.id, "BLOCKED", 1, "test")
    it = await register(MALLORY, "paypay", pp_link("regM2", 1, "ext-bob", "ボブ"))
    check("停止された人の垢は他人も登録できない", store.binding(MALLORY.id, "paypay") is None)

    print("\n[15] 日次上限")
    store.update_config(daily_limit_jpy=12000)
    n = len(PP_CALLS["receive"])
    it = FakeInteraction(ALICE)
    await B.process_p2k(it, pp_link("p2k_limit", 5000, "ext-alice"), None)
    check("上限超過は受け取らない", len(PP_CALLS["receive"]) == n, titles(it))

    print(f"\n結果: {PASS} 件成功 / {FAIL} 件失敗   (データ: {TMP})")
    return FAIL


# ------------------------------------------------------------
# kyash_bridge.claim_and_identify (明細の差分で送信者を特定する部分)
# ------------------------------------------------------------

def test_identify():
    print("\n[K] Kyash 送信者の特定 (明細の差分)")
    own = {"publicId": 1, "token": "t"}
    old = [{"transfer": {"txUUIDAPI": "old1", "amount": 2000,
                         "target": {"publicId": 42, "userName": "someone"}}}]
    new = [{"transfer": {"txUUIDAPI": "new1", "amount": 2000, "createdAt": "2026-11-20T10:00:00+09:00",
                         "target": {"publicId": 5551, "userName": "alice_k", "nameFull": "A"}}}] + old
    calls = {"n": 0}

    def tl(limit=20, **kw):
        calls["n"] += 1
        return old if calls["n"] == 1 else new

    saved = (kyash_api.get_timeline, kyash_api.claim_link, kyash_api.get_account)
    kyash_api.get_timeline = tl
    kyash_api.claim_link = lambda uuid, **kw: ""
    kyash_api.get_account = lambda email=None: own
    try:
        r = REAL_CLAIM("u", 2000, wait_sec=0)
        check("新しく増えた入金の相手を送信者にする",
              r["status"] == "received" and str(r["sender"]["public_id"]) == "5551", r)
        calls["n"] = 0
        both = [{"transfer": {"txUUIDAPI": "new2", "amount": 2000,
                              "target": {"publicId": 999, "userName": "x"}}}] + new
        kyash_api.get_timeline = lambda limit=20, **kw: (old if calls.__setitem__("n", calls["n"] + 1)
                                                         or calls["n"] == 1 else both)
        r = REAL_CLAIM("u", 2000, wait_sec=0)
        check("同額の入金が同時に別人から来たら特定しない", r["status"] == "unidentified", r)

        def bad_claim(uuid, **kw):
            raise kyash_api.KyashError(400, message="already")
        kyash_api.claim_link = bad_claim
        calls["n"] = 0
        kyash_api.get_timeline = tl
        r = REAL_CLAIM("u", 2000, wait_sec=0)
        check("受取APIが400なら failed (受け取っていない)", r["status"] == "failed", r)

        def net_claim(uuid, **kw):
            raise kyash_api.KyashNetworkError("timeout", maybe_sent=True)
        kyash_api.claim_link = net_claim
        calls["n"] = 0
        kyash_api.get_timeline = lambda limit=20, **kw: old
        r = REAL_CLAIM("u", 2000, wait_sec=0)
        check("応答が途切れて明細にも無い → unknown (失敗扱いにしない)", r["status"] == "unknown", r)
    finally:
        kyash_api.get_timeline, kyash_api.claim_link, kyash_api.get_account = saved

    html = ('<img class="profilePicture" src="https://image.kyash.me/11111111-2222-4333-8444-555555555555.jpg?x=1">'
            '<p class="realName">たろう</p>\n<p class="userid"><span>ID：</span>sample_id1</p>')
    p = kyash_api.parse_link_sender(html)
    check("公開ページから Kyash ID を読む", p["sender_user_name"] == "sample_id1" and p["sender_name"] == "たろう", p)
    check("Kyash ID の比較は大文字小文字を無視", aml.same_kyash_id("@Sample_ID1", "sample_id1"))


if __name__ == "__main__":
    test_identify()
    fails = asyncio.run(main())
    sys.exit(1 if FAIL else 0)
