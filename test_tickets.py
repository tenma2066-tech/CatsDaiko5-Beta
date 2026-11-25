"""チケット購入・支援BOXのテスト（PayPay通信はモック）。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as A
import paypay_gateway as G
import pricing as P


class PeriodPrice(unittest.TestCase):
    def test_longer_is_cheaper_per_day(self):
        prices = [P.ticket_period_price(d) for d in range(1, P.TICKET_MAX_DAYS + 1)]
        self.assertEqual(prices[0], P.PRICES["ticket_day"])
        self.assertTrue(all(a < b for a, b in zip(prices, prices[1:])))                  # 長いほど高い（合計）
        per = [p / d for d, p in enumerate(prices, 1)]
        self.assertTrue(all(a > b for a, b in zip(per, per[1:])))                         # 1日あたりは安い
        self.assertTrue(all(p % 10 == 0 for p in prices))
        self.assertEqual(P.ticket_period_price(0), P.PRICES["ticket_unlimited"])
        self.assertLess(prices[-1], P.ticket_period_price(0))
        for bad in (-1, P.TICKET_MAX_DAYS + 1):
            with self.assertRaises(ValueError):
                P.ticket_period_price(bad)

    def test_admin_edit_flows_into_curve(self):
        with patch.dict(P.PRICES, {"ticket_day": 1000}):
            self.assertEqual(P.ticket_period_price(1), 1000)
        with patch.dict(P.PRICES, {"ticket_day": 5000, "ticket_unlimited": 9800}):
            self.assertEqual(P.ticket_period_price(30), 9800)                              # 無制限を超えない


class Base(unittest.TestCase):
    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True),
            patch.dict(A._jobs, {}, clear=True),
            patch.object(G, "history_upsert"),
            patch.object(G, "mark_used"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"),
            patch.object(A, "_rate_ok", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def buy(self, url, body, c=None):
        """注文 → PayPay 入金確認（モック）→ コード発行 まで。"""
        c = c or self.c
        r = c.post(url, json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        with patch.object(G, "receive_payment", AsyncMock(return_value=None)):
            p = c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        self.assertEqual(p.status_code, 200, p.get_data(as_text=True))
        return j, p.get_json()

    def nyanko(self, coupon, c=None, job="J1"):
        self_jobs = A._jobs
        self_jobs[job] = {"finished": None, "created": time.time(), "status": "running", "error": None, "result": None}
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job=job)):
            return (c or self.c).post("/run", json={"chars": "1-5", "items": ["XP"], "coupon": coupon})


class Tickets(Base):
    def test_once_ticket_purchase_issues_free_code(self):
        j, p = self.buy("/api/ticket/order", {"plan": "once"})
        self.assertEqual(j["amount"], A.PRICES["ticket_once"])
        res = p["result"]
        self.assertTrue(res["coupon"].startswith("FR-"))
        self.assertEqual(G.coupon_kind(res["coupon"]), "free")
        self.assertGreater(res["expires"] - time.time(), (A.TICKET_ONCE_DAYS - 1) * 86400)
        o = A._orders[j["order"]]
        self.assertEqual(o["state"], "done")
        # 同じ注文で発行し直しても、同じコード（二重発行しない）
        o["state"] = "paid"
        with A.app.app_context():
            again = A._start_paid_order(o).get_json()
        self.assertEqual(again["result"]["coupon"], res["coupon"])
        # 使うと注文が0円で始まる・2回目は使えない
        r = self.nyanko(res["coupon"])
        self.assertEqual(r.get_json(), {"job": "J1"})
        self.assertEqual(self.nyanko(res["coupon"], job="J2").status_code, 400)

    def test_period_ticket_reusable_one_at_a_time(self):
        j, p = self.buy("/api/ticket/order", {"plan": "period", "days": 7})
        self.assertEqual(j["amount"], A.ticket_period_price(7))
        code = p["result"]["coupon"]
        self.assertEqual(G.coupon_kind(code), "period")
        self.assertAlmostEqual(p["result"]["expires"] - time.time(), 7 * 86400, delta=60)
        self.assertEqual(self.nyanko(code).get_json(), {"job": "J1"})
        first = next(o for o in A._orders.values() if o.get("job_id") == "J1" or o.get("coupon") == code and o["state"] == "started")
        self.assertEqual((first["amount"], first.get("coupon_kind")), (0, "period"))
        self.assertTrue(first["summary"].startswith("【期間チケット】"))
        # 実行中は、ほかの人も同じ人も次の注文に使えない（共有・同時大量注文を防ぐ）
        other = A.app.test_client()
        r = self.nyanko(code, c=other, job="J2")
        self.assertEqual((r.status_code, r.get_json()["code"]), (409, "coupon_busy"))
        # 終わると、また使える。使っても使用済みにならない
        first["state"] = "done"
        self.assertEqual(self.nyanko(code, c=other, job="J3").get_json(), {"job": "J3"})
        self.assertEqual(G.coupon_check(code), (True, ""))

    def test_unlimited_and_expired_period(self):
        _, p = self.buy("/api/ticket/order", {"plan": "period", "days": 0})
        self.assertTrue(p["result"]["unlimited"])
        self.assertGreater(p["result"]["expires"] - time.time(), 3650 * 86400)
        _, p2 = self.buy("/api/ticket/order", {"plan": "period", "days": 1})
        with G._ledger() as db:
            db.execute("UPDATE coupons SET expires=? WHERE code=?", (time.time() - 1, p2["result"]["coupon"]))
        r = self.nyanko(p2["result"]["coupon"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("期限", r.get_json()["error"])

    def test_bad_plans_rejected(self):
        for body in ({"plan": "period", "days": 31}, {"plan": "period", "days": "x"}, {"plan": "nope"}, {}):
            self.assertEqual(self.c.post("/api/ticket/order", json=body).status_code, 400)

    def test_failed_start_does_not_lock_ticket(self):
        _, p = self.buy("/api/ticket/order", {"plan": "period", "days": 3})
        code = p["result"]["coupon"]
        with patch.object(A, "_start_job", side_effect=lambda *a: (A.jsonify(error="full"), 429)):
            r = self.c.post("/run", json={"items": ["XP"], "coupon": code})
        self.assertEqual(r.status_code, 429)
        # 同じ人がもう一度：前の（開始できなかった）注文は閉じて、新しい注文で使える
        self.assertEqual(self.nyanko(code, job="J9").get_json(), {"job": "J9"})

    def test_hold_then_released_issues_code(self):
        r = self.c.post("/api/ticket/order", json={"plan": "once"}).get_json()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
            p = self.c.post(f"/api/orders/{r['order']}/pay", json={"link": self.LINK})
        self.assertTrue(p.get_json()["hold"])
        with patch.object(G, "confirm_received", AsyncMock(return_value=None)), A.app.app_context():
            self.assertEqual(A._recheck_and_start(r["order"], "auto"), "started")
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        o = next(x for x in mine if x["order"] == r["order"])
        self.assertEqual((o["state"], o["product"]), ("done", "ticket_once"))
        self.assertTrue(o["result"]["coupon"].startswith("FR-"))

    def test_tickets_cannot_be_bought_with_coupons_and_maint_blocks(self):
        r = self.c.post("/api/ticket/order", json={"plan": "once", "coupon": "FR-XXXX-XXXX"}).get_json()
        self.assertEqual(r["amount"], A.PRICES["ticket_once"])                              # クーポン欄は無視
        with patch.object(A, "_maint", return_value={"on": True, "message": "", "url": ""}):
            self.assertEqual(self.c.post("/api/ticket/order", json={"plan": "once"}).status_code, 503)


class Support(Base):
    def test_support_gives_30pct_coupon(self):
        j, p = self.buy("/api/support/order", {"amount": 100})
        self.assertEqual(j["amount"], 100)
        code = p["result"]["coupon"]
        self.assertTrue(code.startswith("SP-"))
        self.assertEqual(G.coupon_kind(code), "pct30")
        r = self.c.post("/run", json={"items": ["XP"], "coupon": code}).get_json()           # 25円 → 30%引き（下限なし）
        self.assertEqual(r["amount"], 25 * 70 // 100)
        self.assertTrue(any("30%引き" in b["label"] for b in r["breakdown"]))
        self.assertEqual(G.coupon_check(code)[1], "このクーポンは、ほかのご注文で使用中です。")  # 1回きり（押さえられる）

    def test_each_support_gives_its_own_coupon(self):
        _, a = self.buy("/api/support/order", {"amount": 300})
        _, b = self.buy("/api/support/order", {"amount": 300})
        self.assertNotEqual(a["result"]["coupon"], b["result"]["coupon"])

    def test_support_amount_bounds(self):
        for amt in (99, 0, -5, A.SUPPORT_MAX + 1, "abc", None):
            self.assertEqual(self.c.post("/api/support/order", json={"amount": amt}).status_code, 400)


class Pages(Base):
    def test_pages_and_home(self):
        home = self.c.get("/").get_data(as_text=True)
        for href in ('href="/ticket"', 'href="/support"', "チケット購入🎫", "支援BOX"):
            self.assertIn(href, home)
        t = self.c.get("/ticket").get_data(as_text=True)
        self.assertIn("1回なんでも無料チケット", t)
        self.assertIn(f"{A.PRICES['ticket_once']:,}円", t)
        self.assertIn('id="chart"', t)
        s = self.c.get("/support").get_data(as_text=True)
        self.assertIn("30%引きクーポン", s)

    def test_product_orders_stay_out_of_shop_lists_and_qa(self):
        self.c.post("/api/ticket/order", json={"plan": "once"})
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual([(o["product"], o["tsum"], o["puni"]) for o in mine], [("ticket_once", False, False)])
        with self.c.session_transaction():
            pass
        with A.app.test_request_context(), patch.object(A, "_owner", return_value=A._orders[mine[0]["order"]]["owner"]):
            self.assertEqual(A._qa_orders_for("nyanko"), [])

    def test_admin_marks_product_paid_issues_code(self):
        r = self.c.post("/api/support/order", json={"amount": 500}).get_json()
        o = A._orders[r["order"]]
        o["state"] = "paid"
        with A.app.app_context():
            out = A._start_paid_order(o)
        self.assertEqual(o["state"], "done")
        self.assertTrue(out.get_json()["result"]["coupon"].startswith("SP-"))


if __name__ == "__main__":
    unittest.main()
