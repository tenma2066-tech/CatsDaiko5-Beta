"""決済してから実行する流れのテスト（PayPay通信はモック）。"""
import unittest
from unittest.mock import patch, AsyncMock

import app as A
import paypay_gateway as G


class OrderFlow(unittest.TestCase):
    def setUp(self):
        self.p = patch.object(A, "PAYMENTS_ENABLED", True)
        self.p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        self.p.stop()

    def order(self, c=None):
        r = (c or self.c).post("/run", json={"items": ["XP"], "dry": True})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_run_returns_order_not_job(self):
        j = self.order()
        self.assertEqual(j["amount"], 20)
        self.assertNotIn("job", j)

    def test_prices(self):
        f = A.compute_amount
        self.assertEqual(f(["XP"], [], [], {}, [], False, False)[0], 20)
        self.assertEqual(f(["XP"], ["全キャラレベルMAX"], [], {"XP": 5, "NP": 1}, [], False, False)[0], 60)  # XP/NP/サブ=3件
        self.assertEqual(f([], [], [], {}, [1, 2, 3], False, False)[0], 150)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS), {}, [], False, False)[0], 100)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS) + ["施設全強化"], {}, [7], False, False)[0], 170)
        self.assertEqual(f([], [], [], {}, [], True, False)[0], 100)
        self.assertEqual(f([], [], [], {}, [], False, True)[0], 500)

    def test_order_amount_is_server_side(self):
        r = self.c.post("/run", json={"chars": "1-3", "amount": 1}).get_json()
        self.assertEqual(r["amount"], 150)
        r = self.c.post("/run", json={"transfer": "abc", "confirm": "1234", "integrity_only": True}).get_json()
        self.assertEqual(r["amount"], 500)
        r = self.c.post("/run", json={"transfer": "abc", "confirm": "1234", "clone": True}).get_json()
        self.assertEqual(r["amount"], 100)

    def test_unpaid_failure_does_not_start(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("amount_mismatch", "金額が違います"))), \
             patch.object(A, "_start_job") as start:
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r.status_code, 402)
        start.assert_not_called()
        self.assertFalse(r.get_json()["uncertain"])

    def test_uncertain_kills_order(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("accept_uncertain", "不明"))):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertTrue(r.get_json()["uncertain"])
        r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r.status_code, 409)

    def test_other_session_cannot_use_order(self):
        j = self.order()
        r = A.app.test_client().post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r.status_code, 404)

    def test_paid_starts_job_once(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")) as rp, \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J1")):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
            self.assertEqual(r.get_json()["job"], "J1")
            r2 = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r2.status_code, 409)
        rp.assert_awaited_once()

    def test_paid_but_busy_retries_without_repay(self):
        j = self.order()
        busy = lambda *a: (A.jsonify(error="busy"), 429)
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")) as rp:
            with patch.object(A, "_start_job", side_effect=busy):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
            self.assertEqual((r.status_code, r.get_json()["paid"]), (429, True))
            with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J2")):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
            self.assertEqual(r.get_json()["job"], "J2")
        rp.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
