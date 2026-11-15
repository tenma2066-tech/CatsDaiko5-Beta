"""保留→送金者が解除→同じリンクで再受け取り、の流れ（偽のPayPayで検証）。"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

import paypay_gateway as G

AMOUNT = 175


def link_body(status="PENDING", amount=AMOUNT):
    now = datetime.now(timezone.utc)
    return {"header": {"resultCode": "S0000"}, "payload": {
        "orderStatus": status,
        "pendingP2PInfo": {"amount": amount, "isLinkBlocked": False, "isSetPasscode": False,
                           "expiredAt": (now + timedelta(days=1)).isoformat(), "orderId": "prov-1"},
        "sender": {"externalId": "s"}, "receiver": {"externalId": "r"},
        "message": {"messageId": "m", "chatRoomId": "c",
                    "data": {"orderId": "prov-1", "amount": amount, "status": "PENDING", "transactionAt": now.isoformat()}}}}


HOLD_REPLY = {"header": {"resultCode": "42007013"}, "payload": None}
OK_REPLY = {"header": {"resultCode": "S0000"}, "payload": {"orderStatus": "COMPLETED"}, "error": None}


class FakePayPay:
    """getP2PLinkInfo と acceptP2PSendMoneyLink を順番どおりに返す。"""
    def __init__(self, gets, posts):
        self.gets, self.posts, self.post_calls = list(gets), list(posts), 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resp(self, body):
        outer = self

        class R:
            status = 200
            async def json(self, content_type=None):
                return body
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
        return R()

    def get(self, *a, **k):
        return self._resp(self.gets.pop(0))

    def post(self, *a, **k):
        self.post_calls += 1
        return self._resp(self.posts.pop(0))


class HoldReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
                  patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": ""}),
                  patch.object(G, "_fresh_account", AsyncMock(return_value={"uuid": "u", "session_cookie": "c"}))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def run_with(self, fake, coro):
        with patch.object(G.aiohttp, "ClientSession", fake):
            return asyncio.run(coro)

    def test_hold_then_sender_releases_then_accepted_again(self):
        link = "https://pay.paypay.ne.jp/abcDEF123456"
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake, G.receive_payment(link, AMOUNT, "order-1"))
        self.assertEqual(e.exception.code, "temporary_hold")
        # 送金者が解除 → リンクは PENDING のまま → 再確認で同じリンクをもう一度受け取る
        fake2 = FakePayPay(gets=[link_body("PENDING")], posts=[OK_REPLY])
        digest = self.run_with(fake2, G.confirm_received("abcDEF123456", "order-1", AMOUNT))
        self.assertTrue(digest)
        self.assertEqual(fake2.post_calls, 1)
        # 以後は受け取り済み扱い（PayPayに問い合わせない）
        self.assertEqual(asyncio.run(G.confirm_received("abcDEF123456", "order-1", AMOUNT)), digest)

    def test_still_held_stays_hold(self):
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError):
            self.run_with(fake, G.receive_payment("abcDEF123456", AMOUNT, "order-2"))
        fake2 = FakePayPay(gets=[link_body("PENDING")], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake2, G.confirm_received("abcDEF123456", "order-2", AMOUNT))
        self.assertEqual(e.exception.code, "temporary_hold")

    def test_completed_by_receiver_app_is_paid_without_post(self):
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError):
            self.run_with(fake, G.receive_payment("abcDEF123456", AMOUNT, "order-3"))
        fake2 = FakePayPay(gets=[link_body("COMPLETED")], posts=[])
        self.assertTrue(self.run_with(fake2, G.confirm_received("abcDEF123456", "order-3", AMOUNT)))
        self.assertEqual(fake2.post_calls, 0)

    def _held(self, order):
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError):
            self.run_with(fake, G.receive_payment("abcDEF123456", AMOUNT, order))

    def test_success_status_counts_as_received(self):
        """受け取り済みのリンクは orderStatus=SUCCESS で返る（COMPLETED ではない）。"""
        self._held("order-s")
        fake = FakePayPay(gets=[link_body("SUCCESS")], posts=[])
        self.assertTrue(self.run_with(fake, G.confirm_received("abcDEF123456", "order-s", AMOUNT)))
        self.assertEqual(fake.post_calls, 0)
        self.assertIn("paid", G.link_diag("abcDEF123456"))

    def test_success_received_by_someone_else_is_not_paid(self):
        self._held("order-x")
        body = link_body("SUCCESS")
        body["payload"]["receiver"]["externalId"] = "another-account"
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakePayPay(gets=[body], posts=[]), G.confirm_received("abcDEF123456", "order-x", AMOUNT))
        self.assertEqual(e.exception.code, "hold_failed")

    def test_unknown_status_keeps_waiting_and_is_recorded(self):
        self._held("order-u")
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakePayPay(gets=[link_body("PROCESSING")], posts=[]), G.confirm_received("abcDEF123456", "order-u", AMOUNT))
        self.assertEqual(e.exception.code, "still_pending")
        self.assertIn("PROCESSING", G.link_diag("abcDEF123456"))

    def test_cancelled_is_terminal(self):
        self._held("order-c")
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(FakePayPay(gets=[link_body("CANCELED")], posts=[]), G.confirm_received("abcDEF123456", "order-c", AMOUNT))
        self.assertEqual(e.exception.code, "hold_failed")

    def test_admin_mark_paid(self):
        self._held("order-a")
        G.mark_paid_by_admin("abcDEF123456", "order-a")
        self.assertTrue(asyncio.run(G.confirm_received("abcDEF123456", "order-a", AMOUNT)))

    def test_amount_changed_is_rejected_on_recheck(self):
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError):
            self.run_with(fake, G.receive_payment("abcDEF123456", AMOUNT, "order-4"))
        fake2 = FakePayPay(gets=[link_body("PENDING", amount=1)], posts=[OK_REPLY])
        with self.assertRaises(G.PayPayError) as e:
            self.run_with(fake2, G.confirm_received("abcDEF123456", "order-4", AMOUNT))
        self.assertEqual(e.exception.code, "amount_mismatch")
        self.assertEqual(fake2.post_calls, 0)

    def test_other_order_cannot_reuse_link(self):
        fake = FakePayPay(gets=[link_body()], posts=[HOLD_REPLY])
        with self.assertRaises(G.PayPayError):
            self.run_with(fake, G.receive_payment("abcDEF123456", AMOUNT, "order-5"))
        with self.assertRaises(G.PayPayError) as e:
            asyncio.run(G.confirm_received("abcDEF123456", "someone-else", AMOUNT))
        self.assertEqual(e.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
