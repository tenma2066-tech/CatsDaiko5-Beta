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
        self.assertEqual(j["amount"], 25)
        self.assertNotIn("job", j)

    def test_prices(self):
        f = A.compute_amount
        self.assertEqual(f(["XP"], [], [], {}, [], False, False)[0], 25)
        self.assertEqual(f(["XP"], ["全キャラレベルMAX"], [], {"XP": 5, "NP": 1}, [], False, False)[0], 75)  # XP/NP/サブ=3件
        self.assertEqual(f([], [], [], {}, [1, 2, 3], False, False)[0], 150)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS), {}, [], False, False)[0], 100)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS) + ["施設全強化"], {}, [7], False, False)[0], 175)
        # キャラ: 1番=レベル+形態+本能(3件), 2番=設定なし(1件), 3番=+値のみ(1件) → 5件=250円
        cs = {0: {"base_level": 50, "form": 3, "talents_all": True}, 2: {"plus_level": 10}}
        self.assertEqual(f([], [], [], {}, [1, 2, 3], False, False, cs)[0], 250)
        self.assertEqual(f([], [], [], {}, [], True, False)[0], 100)
        self.assertEqual(f([], [], [], {}, [], False, True)[0], 500)

    def test_order_amount_is_server_side(self):
        r = self.c.post("/run", json={"chars": "1-3", "amount": 1}).get_json()
        self.assertEqual(r["amount"], 150)
        r = self.c.post("/run", json={"chars": "1", "cat_settings": {"1": {"base_level": 30, "form": 2}}}).get_json()
        self.assertEqual(r["amount"], 100)
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

    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def test_hold_then_received_starts_job_without_new_link(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        self.assertEqual(r.status_code, 402)
        self.assertTrue(r.get_json()["hold"])
        # まだ受け取られていない → 保留のまま
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("still_pending", "未"))):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual((r.status_code, r.get_json()["hold"], r.get_json()["code"]), (402, True, "still_pending"))
        # 解除待ちの確認を何度しても「失敗が続いた」でロックされない
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("still_pending", "未"))):
            for _ in range(8):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual(r.get_json()["code"], "still_pending")
        # 受け取り済み → 開始（新しいリンクもreceive_paymentも使わない）
        with patch.object(G, "confirm_received", AsyncMock(return_value="d")) as cr, \
             patch.object(G, "receive_payment", AsyncMock()) as rp, \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J3")):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "https://pay.paypay.ne.jp/otherLINK9999"})
        self.assertEqual(r.get_json()["job"], "J3")
        cr.assert_awaited_once()
        rp.assert_not_awaited()

    def test_hold_failed_kills_order(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("accept_uncertain", "不明"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("hold_failed", "x"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={})
        r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
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



class OrderPersistence(unittest.TestCase):
    def test_roundtrip_survives_restart_and_resumes_hold(self):
        import os, tempfile
        from pathlib import Path
        from cryptography.fernet import Fernet
        with tempfile.TemporaryDirectory() as d, \
             patch.dict(os.environ, {"PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode(), "RUNTIME_GITHUB_REPO": ""}), \
             patch.object(G, "_ROOT", Path(d)), patch.object(G, "_ORDERS_FILE", Path(d) / "orders.enc"), \
             patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True):
            c = A.app.test_client()
            j = c.post("/run", json={"transfer": "abcd1234", "confirm": "1234", "qty": {"XP": "5"}, "chars": "1", "cat_settings": {"1": {"form": 2}}}).get_json()
            link = "https://pay.paypay.ne.jp/abcDEF123456"
            with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
                self.assertTrue(c.post(f"/api/orders/{j['order']}/pay", json={"link": link}).get_json()["hold"])
            self.assertNotIn(b"abcd1234", (Path(d) / "orders.enc").read_bytes())  # 暗号化されている
            # 再起動をシミュレート
            A._orders.clear()
            A._load_orders()
            self.assertEqual(A._orders[j["order"]]["state"], "hold")
            self.assertEqual(A._orders[j["order"]]["arguments"][7], {0: {"form": 2}})
            with patch.object(G, "confirm_received", AsyncMock(return_value="d")), \
                 patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J9")):
                r = c.post(f"/api/orders/{j['order']}/pay", json={})
            self.assertEqual(r.get_json()["job"], "J9")

    def test_paying_at_crash_becomes_hold(self):
        o = {"state": "paying", "link_code": "abcdef", "arguments": None, "transfer_key": None}
        self.assertEqual(A._order_from_json(o)["state"], "hold")
        o["link_code"] = None
        self.assertEqual(A._order_from_json(o)["state"], "unpaid")


if __name__ == "__main__":
    unittest.main()
