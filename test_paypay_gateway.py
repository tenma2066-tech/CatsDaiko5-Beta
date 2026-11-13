import unittest
from datetime import datetime, timedelta, timezone

import paypay_gateway


class PayPayGatewayValidationTests(unittest.TestCase):
    def test_normalize_paypay_link(self):
        self.assertEqual(paypay_gateway._normalize_link("https://pay.paypay.ne.jp/AbCd123456"),
                         "AbCd123456")
        self.assertEqual(paypay_gateway._normalize_link("AbCd123456"), "AbCd123456")

    def test_rejects_untrusted_or_ambiguous_links(self):
        self.assertIsNone(paypay_gateway._normalize_link("https://evil.example/AbCd123456"))
        self.assertIsNone(paypay_gateway._normalize_link(
            "https://pay.paypay.ne.jp/AbCd123456 https://evil.example/"))
        self.assertIsNone(paypay_gateway._normalize_link("https://pay.paypay.ne.jp/a"))

    def _valid_body(self, amount=1200):
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        return {
            "header": {"resultCode": "S0000"},
            "payload": {
                "orderStatus": "PENDING",
                "pendingP2PInfo": {
                    "amount": amount, "isLinkBlocked": False, "isSetPasscode": False,
                    "expiredAt": expiry, "orderId": "provider-order-1"
                },
                "sender": {"externalId": "sender"},
                "receiver": {"externalId": "receiver"},
                "message": {
                    "messageId": "message-1", "chatRoomId": "chat-1",
                    "data": {"orderId": "provider-order-1", "amount": amount,
                             "status": "PENDING", "transactionAt": datetime.now(timezone.utc).isoformat()}
                }
            }
        }

    def test_rejects_extra_path_segments_and_multiple_trailing_slashes(self):
        self.assertIsNone(paypay_gateway._normalize_link("https://pay.paypay.ne.jp/AbCd123456/extra"))
        self.assertIsNone(paypay_gateway._normalize_link("https://pay.paypay.ne.jp/AbCd123456//"))

    def test_structured_temporary_hold_code_is_detected(self):
        self.assertEqual(paypay_gateway._provider_hold_code(
            {"error": {"errorCode": "42007013"}}
        ), "42007013")
        self.assertIsNone(paypay_gateway._provider_hold_code(
            {"message": "42007013"}
        ))

    def test_valid_pending_link_matches_server_amount(self):
        result = paypay_gateway._verify_link(self._valid_body(), 1200)
        self.assertEqual(result["order_id"], "provider-order-1")

    def test_amount_mismatch_rejected(self):
        with self.assertRaises(paypay_gateway.PayPayError) as error:
            paypay_gateway._verify_link(self._valid_body(1201), 1200)
        self.assertEqual(error.exception.code, "amount_mismatch")

    def test_expired_link_rejected(self):
        body = self._valid_body()
        body["payload"]["pendingP2PInfo"]["expiredAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        with self.assertRaises(paypay_gateway.PayPayError) as error:
            paypay_gateway._verify_link(body, 1200)
        self.assertEqual(error.exception.code, "expired")


if __name__ == "__main__":
    unittest.main()
