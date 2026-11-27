"""Kyash の受け取り（kyash_gateway）と、PayPay／Kyash の振り分け（payments）のテスト。Kyash との通信はモック。"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import kyash_api as ka
import kyash_gateway as K
import payments as P
import paypay_gateway as G

LINK = "https://kyash.me/payments/AbCdEf123"
CODE = "ky:AbCdEf123"
UUID = "11111111-2222-3333-4444-555555555555"
AMOUNT = 150
ACCOUNT = {"email": "shop@example.com", "token": "t", "device": {"x": "y"}, "user_name": "shop", "public_id": 1}


def page(uuid=UUID, amount=AMOUNT, status=200, sender="buyer"):
    return {"amount": amount, "uuid": uuid, "http_status": status, "sender_user_name": sender, "sender_name": "買う人"}


def tx(txid, amount=AMOUNT, at="2099-01-01T00:00:00+00:00"):
    return {"transfer": {"amount": amount, "txUUIDAPI": txid, "createdAt": at}}


class FakeKyash:
    """resolve_kyash_link / get_timeline / claim_link を順番どおりに返す。"""
    def __init__(self, pages, timelines, claim=None):
        self.pages, self.timelines, self.claim = list(pages), list(timelines), claim
        self.claims = 0

    def resolve(self, code):
        return self.pages.pop(0)

    def timeline(self, limit, token, device):
        return self.timelines.pop(0) if len(self.timelines) > 1 else self.timelines[0]

    def claim_link(self, uuid, token, device):
        self.claims += 1
        if isinstance(self.claim, Exception):
            raise self.claim
        return None


class KyashGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
                  patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": ""}),
                  patch.object(K, "_account", lambda force=False: dict(ACCOUNT)),
                  patch.object(K, "CLAIM_WAIT_SEC", 0),
                  patch.object(K.time, "sleep", lambda s: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def run_with(self, fake, coro):
        with patch.object(ka, "resolve_kyash_link", fake.resolve), patch.object(ka, "get_timeline", fake.timeline), \
             patch.object(ka, "claim_link", fake.claim_link):
            return asyncio.run(coro)

    def row(self, code=CODE):
        with G._ledger() as db:
            return db.execute("SELECT * FROM links WHERE digest=?", (K._digest(code),)).fetchone()

    def test_normalize_and_detect(self):
        self.assertEqual(K.normalize_link(LINK), CODE)
        self.assertEqual(K.normalize_link(LINK + "  "), CODE)
        self.assertIsNone(K.normalize_link("https://evil.example/kyash.me/payments/AbCdEf123"))
        self.assertIsNone(K.normalize_link(LINK + " https://kyash.me/payments/Other1234"))
        self.assertEqual(P.detect(LINK), "kyash")
        self.assertEqual(P.detect("https://pay.paypay.ne.jp/abcDEF123456"), "paypay")
        self.assertIsNone(P.detect("hello"))
        self.assertEqual(P.normalize(LINK), CODE)
        self.assertEqual(P.normalize("https://pay.paypay.ne.jp/abcDEF123456"), "abcDEF123456")
        self.assertEqual(P.provider_of(CODE), "kyash")
        self.assertEqual(P.provider_of("abcDEF123456"), "paypay")
        self.assertTrue(G.LEDGER_CODE_RE.fullmatch(CODE))

    def test_received_when_new_incoming_appears(self):
        fake = FakeKyash([page()], [[tx("old")], [tx("old"), tx("new")]])
        self.assertTrue(self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1")))
        self.assertEqual(fake.claims, 1)
        r = self.row()
        self.assertEqual((r["state"], r["provider"], r["order_id"]), ("paid", "kyash", "o1"))
        self.assertEqual(json.loads(r["extra"])["tx"], "new")
        # 同じリンクを同じ注文で貼り直しても、受け取り直さない
        self.assertTrue(self.run_with(FakeKyash([], [[]]), K.receive_payment(LINK, AMOUNT, "o1")))
        # 別の注文では使えない
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakeKyash([], [[]]), K.receive_payment(LINK, AMOUNT, "o2"))
        self.assertEqual(e.exception.code, "duplicate")

    def test_amount_mismatch_does_not_claim(self):
        fake = FakeKyash([page(amount=AMOUNT + 1)], [[]])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "amount_mismatch")
        self.assertEqual(fake.claims, 0)
        self.assertIsNone(self.row())

    def test_claimed_or_cancelled_link_is_refused(self):
        fake = FakeKyash([page(uuid=None)], [[]])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "link_cancelled")
        self.assertEqual(fake.claims, 0)

    def test_own_link_is_refused(self):
        fake = FakeKyash([page(sender="shop")], [[]])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "identity")
        self.assertEqual(fake.claims, 0)

    def test_not_sent_claim_can_be_retried_with_same_link(self):
        fake = FakeKyash([page()], [[]], claim=ka.KyashNetworkError("接続できない", maybe_sent=False))
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "ky_network")
        self.assertIsNone(self.row())   # 台帳に残さない → 同じリンクでやり直せる
        fake2 = FakeKyash([page()], [[], [tx("new")]])
        self.assertTrue(self.run_with(fake2, K.receive_payment(LINK, AMOUNT, "o1")))

    def test_rejected_claim_is_not_recorded(self):
        fake = FakeKyash([page()], [[]], claim=ka.KyashError(400, message="受け取れない"))
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "ky_claim_rejected")
        self.assertIsNone(self.row())

    def test_uncertain_claim_then_found_on_recheck(self):
        fake = FakeKyash([page()], [[tx("old")]], claim=ka.KyashNetworkError("途中で切れた", maybe_sent=True))
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "ky_unconfirmed")
        self.assertEqual(self.row()["state"], "review")
        # 明細に出た → 支払い済み（受け取りは投げ直さない）
        fake2 = FakeKyash([], [[tx("old"), tx("new")]])
        self.assertTrue(self.run_with(fake2, K.confirm_received(CODE, "o1", AMOUNT)))
        self.assertEqual(fake2.claims, 0)
        self.assertEqual(self.row()["state"], "paid")

    def test_claimed_but_not_in_timeline_yet_waits(self):
        fake = FakeKyash([page()], [[tx("old")]])   # 受け取りは 200、でも明細に出ない
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "ky_unconfirmed")
        # リンクはもう受け取れない状態・明細にもまだ無い → 待つ
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakeKyash([page(uuid=None)], [[tx("old")]]), K.confirm_received(CODE, "o1", AMOUNT))
        self.assertEqual(e.exception.code, "ky_unconfirmed")
        # 30分たっても見つからない → 管理者確認へ
        extra = json.loads(self.row()["extra"])
        extra["claimed_at"] -= K.GIVE_UP_SEC + 10
        K._set(K._digest(CODE), extra=extra)
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakeKyash([page(uuid=None)], [[tx("old")]]), K.confirm_received(CODE, "o1", AMOUNT))
        self.assertEqual(e.exception.code, "hold_failed")

    def test_recheck_claims_again_when_link_still_pending(self):
        fake = FakeKyash([page()], [[]], claim=ka.KyashError(503, message="混雑"))
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "ky_unconfirmed")
        fake2 = FakeKyash([page()], [[], [], [tx("new")]])
        self.assertTrue(self.run_with(fake2, K.confirm_received(CODE, "o1", AMOUNT)))
        self.assertEqual(fake2.claims, 1)

    def test_one_incoming_is_not_used_for_two_orders(self):
        self.assertTrue(self.run_with(FakeKyash([page()], [[], [tx("t1")]]), K.receive_payment(LINK, AMOUNT, "o1")))
        other = "https://kyash.me/payments/Zz9988776"
        with self.assertRaises(G.PayPayError) as e:   # 明細には o1 の入金しかない → o2 には充てない
            self.run_with(FakeKyash([page()], [[], [tx("t1")]]), K.receive_payment(other, AMOUNT, "o2"))
        self.assertEqual(e.exception.code, "ky_unconfirmed")

    def test_ledger_helpers_accept_kyash_codes(self):
        self.run_with(FakeKyash([page()], [[], [tx("t1")]]), K.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(G.link_order_id(CODE), "o1")
        self.assertTrue(G.link_diag(CODE).startswith("paid"))
        G.mark_used(CODE)
        self.assertEqual(self.row()["used"], 1)


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch.object(P, "_FILE", os.path.join(self.tmp.name, "pay_methods.json"))]
        for x in self.p:
            x.start()
        P.forget()

    def tearDown(self):
        for x in self.p:
            x.stop()
        P.forget()
        self.tmp.cleanup()

    def test_defaults_and_save(self):
        self.assertEqual(P.switches(), {"paypay": True, "kyash": True})
        P.set_switches({"paypay": False, "kyash": True})
        self.assertEqual(P.switches(), {"paypay": False, "kyash": True})

    def test_available_needs_switch_and_connection(self):
        with patch.object(G, "status", return_value={"connected": True}), \
             patch.object(K, "status", return_value={"connected": False, "needs_admin": False}):
            self.assertEqual(P.available(), {"paypay": True, "kyash": False})
        P.forget()
        P.set_switches({"paypay": False, "kyash": True})
        with patch.object(G, "status", return_value={"connected": True}), \
             patch.object(K, "status", return_value={"connected": True, "needs_admin": False}):
            self.assertEqual(P.available(), {"paypay": False, "kyash": True})

    def test_switched_off_method_is_refused_but_recheck_still_works(self):
        P.set_switches({"paypay": True, "kyash": False})
        with patch.object(G, "status", return_value={"connected": True}), \
             patch.object(K, "receive_payment", AsyncMock()) as kr:
            with self.assertRaises(G.PayPayError) as e:
                asyncio.run(P.receive_payment(LINK, AMOUNT, "o1"))
        self.assertEqual(e.exception.code, "method_off")
        kr.assert_not_called()
        with patch.object(K, "confirm_received", AsyncMock(return_value="d")) as kc:
            self.assertEqual(asyncio.run(P.confirm_received(CODE, "o1", AMOUNT)), "d")
        kc.assert_called_once()

    def test_dispatch(self):
        with patch.object(K, "receive_payment", AsyncMock(return_value="k")), \
             patch.object(G, "receive_payment", AsyncMock(return_value="p")):
            self.assertEqual(asyncio.run(P.receive_payment(LINK, AMOUNT, "o1")), "k")
            self.assertEqual(asyncio.run(P.receive_payment("https://pay.paypay.ne.jp/abcDEF123456", AMOUNT, "o2")), "p")
        with self.assertRaises(G.PayPayError) as e:
            asyncio.run(P.receive_payment("hello", AMOUNT, "o3"))
        self.assertEqual(e.exception.code, "not_paypay_link")


class AccountTests(unittest.TestCase):
    """受取用アカウントの保存（暗号化）と、トークン切れの扱い。"""
    def setUp(self):
        from cryptography.fernet import Fernet
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": "", "PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode()}),
                  patch.object(K, "_ROOT", Path(self.tmp.name)),
                  patch.object(K, "_ACCOUNT_FILE", Path(self.tmp.name) / "account.enc"),
                  patch.object(ka, "get_me", return_value={"publicId": 7, "userName": "shop"})]
        for x in self.p:
            x.start()
        K._pending_login.clear()
        K._last.update(error="", code="", at=0.0)

    def tearDown(self):
        for x in self.p:
            x.stop()
        K._pending_login.clear()
        K._last.update(error="", code="", at=0.0)
        self.tmp.cleanup()

    def test_login_with_verify_link_and_encrypted_storage(self):
        with patch.object(ka, "login_password", return_value={"verified": False, "token": "prov"}):
            self.assertTrue(K.begin_login("shop@example.com", "pw")["verify_required"])
        with patch.object(ka, "login_verify_link", return_value={"token": "tok", "refreshToken": "rt", "tokenExpiry": "2099-01-01T00:00:00Z"}):
            K.finish_login("https://kyash.me/login?token=abc")
        self.assertNotIn(b"tok", (Path(self.tmp.name) / "account.enc").read_bytes())
        st = K.status()
        self.assertTrue(st["connected"] and st["ready"])
        self.assertEqual(st["user_name"], "shop")

    def test_401_refreshes_once_then_retries(self):
        with patch.object(ka, "login_password", return_value={"verified": True, "token": "old", "refreshToken": "rt", "tokenExpiry": "2099-01-01T00:00:00Z"}):
            K.begin_login("shop@example.com", "pw")
        calls = []

        def fn(token, device):
            calls.append(token)
            if token == "old":
                raise ka.KyashError(401, message="expired")
            return "ok"
        with patch.object(ka, "_request", return_value={"token": "new", "refreshToken": "rt2"}):
            self.assertEqual(K._call(fn), "ok")
        self.assertEqual(calls, ["old", "new"])

    def test_verify_needed_on_relogin_hides_kyash(self):
        with patch.object(ka, "login_password", return_value={"verified": True, "token": "old", "refreshToken": "", "tokenExpiry": "2099-01-01T00:00:00Z"}):
            K.begin_login("shop@example.com", "pw")
        with patch.object(ka, "login_password", return_value={"verified": False, "token": "prov"}):
            with self.assertRaises(G.PayPayError) as e:
                K._account(force=True)
        self.assertEqual(e.exception.code, "ky_verify_required")
        self.assertTrue(K.status()["needs_admin"])
        P.forget()
        with patch.object(G, "status", return_value={"connected": False}):
            self.assertFalse(P.available()["kyash"])
        P.forget()


if __name__ == "__main__":
    unittest.main()


class AppFlowTests(unittest.TestCase):
    """お店の画面・管理画面からの流れ（Kyash のリンク／スイッチ）。"""
    def setUp(self):
        import app as A
        self.A = A
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True),
                  patch.dict(A._jobs, {}, clear=True), patch.object(G, "history_upsert"),
                  patch.object(P, "_FILE", os.path.join(self.tmp.name, "pay_methods.json"))]
        for x in self.p:
            x.start()
        P.forget()
        self.c = A.app.test_client()

    def tearDown(self):
        for x in self.p:
            x.stop()
        P.forget()
        self.tmp.cleanup()

    def order(self):
        r = self.c.post("/run", json={"items": ["XP"], "dry": True})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["order"]

    def test_kyash_link_goes_to_kyash_and_hold_is_rechecked_by_server(self):
        oid = self.order()
        with patch.object(K, "receive_payment", AsyncMock(side_effect=G.PayPayError("ky_unconfirmed", "反映待ち"))) as kr, \
             patch.object(G, "receive_payment", AsyncMock()) as pr:
            r = self.c.post(f"/api/orders/{oid}/pay", json={"link": LINK})
        self.assertEqual((r.status_code, r.get_json()["hold"]), (402, True))
        kr.assert_awaited_once()
        pr.assert_not_awaited()
        self.assertEqual((self.A._orders[oid]["state"], self.A._orders[oid]["link_code"]), ("hold", CODE))
        # 保留の自動確認（サーバー側）→ Kyash の再確認に回り、入金済みなら開始
        with patch.object(K, "confirm_received", AsyncMock(return_value="d")) as kc, \
             patch.object(G, "confirm_received", AsyncMock()) as pc, \
             patch.object(self.A, "_start_job", side_effect=lambda *a: self.A.jsonify(job="JK")), patch.object(G, "mark_used"):
            self.assertEqual(self.A._recheck_and_start(oid, "auto"), "started")
        kc.assert_awaited_once()
        pc.assert_not_awaited()
        self.assertEqual(self.A._orders[oid]["state"], "started")

    def test_switched_off_paypay_refuses_paypay_link_without_holding_order(self):
        P.set_switches({"paypay": False, "kyash": True})
        oid = self.order()
        with patch.object(G, "receive_payment", AsyncMock()) as pr, \
             patch.object(K, "status", return_value={"connected": True, "needs_admin": False}):
            r = self.c.post(f"/api/orders/{oid}/pay", json={"link": "https://pay.paypay.ne.jp/abcDEF123456"})
        self.assertEqual((r.status_code, r.get_json()["code"], r.get_json()["hold"]), (402, "method_off", False))
        self.assertIn("Kyash", r.get_json()["error"])
        pr.assert_not_awaited()
        self.assertEqual(self.A._orders[oid]["state"], "unpaid")
        self.assertIsNone(self.A._orders[oid]["link_code"])

    def test_admin_switch_and_kyash_card(self):
        A = self.A
        admin = A.app.test_client()
        with patch.object(A, "ADMIN_PASSWORD", "adm"), patch.object(G, "status", return_value={"connected": True, "ready": True, "expires_at": 0}), \
             patch.object(K, "status", return_value={"connected": True, "ready": True, "expires_at": 4102444800, "user_name": "shopid",
                                                      "email": "s@example.com", "verify_waiting": False, "last_error": "", "needs_admin": False}):
            admin.get("/admin")
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            admin.post("/admin", data={"action": "unlock", "csrf": csrf, "passcode": "adm"})
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            page = admin.get("/admin").get_data(as_text=True)
            self.assertIn("Kyash 受取アカウント", page)
            self.assertIn("shopid", page)
            page = admin.post("/admin", follow_redirects=True, data={"action": "paymethods_save", "csrf": csrf, "kyash": "1"}).get_data(as_text=True)
            self.assertIn("受け付ける決済方法を保存しました（Kyash）", page)
            self.assertEqual(P.switches(), {"paypay": False, "kyash": True})
            # お客さんの支払い画面には、いま使える方法が渡る
            P.forget()
            html = self.c.get("/nyanko").get_data(as_text=True)
            self.assertIn("/static/paymethods.js", html)
            self.assertIn("&#34;kyash&#34;: true", html)
            self.assertIn("&#34;paypay&#34;: false", html)


class PayNoticeTests(unittest.TestCase):
    """「Kyashの決済に移行しました」のお知らせ：管理者以外に、Kyash を受け付けているときだけ。"""
    PAGES = ("/", "/nyanko", "/tsum", "/puni", "/ticket", "/support", "/source")

    def setUp(self):
        import app as A
        self.A = A
        self.p = patch.object(A, "PAYMENTS_ENABLED", True)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def pages(self, client, av):
        with patch.object(P, "available", return_value=av):
            return {u: client.get(u).get_data(as_text=True) for u in self.PAGES}

    def test_shown_to_customers_on_every_shop_page(self):
        for u, html in self.pages(self.A.app.test_client(), {"paypay": True, "kyash": True}).items():
            self.assertIn('/static/paynotice.js?v=1" data-paypay="1"', html, u)
        for u, html in self.pages(self.A.app.test_client(), {"paypay": False, "kyash": True}).items():
            self.assertIn('/static/paynotice.js?v=1" data-paypay="0"', html, u)

    def test_hidden_when_kyash_is_off(self):
        for u, html in self.pages(self.A.app.test_client(), {"paypay": True, "kyash": False}).items():
            self.assertNotIn("paynotice.js", html, u)

    def test_hidden_for_admin(self):
        admin = self.A.app.test_client()
        with patch.object(self.A, "_is_admin", return_value=True):
            for u, html in self.pages(admin, {"paypay": True, "kyash": True}).items():
                self.assertNotIn("paynotice.js", html, u)
