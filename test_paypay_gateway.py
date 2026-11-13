import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

import github_runtime_store
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

    def test_payment_link_digest_is_stable_for_code_and_url(self):
        code_digest = paypay_gateway.payment_link_digest("AbCd123456")
        url_digest = paypay_gateway.payment_link_digest("https://pay.paypay.ne.jp/AbCd123456")
        self.assertEqual(code_digest, url_digest)
        self.assertEqual(len(code_digest), 64)
        with self.assertRaises(paypay_gateway.PayPayError):
            paypay_gateway.payment_link_digest("https://evil.example/AbCd123456")

    def test_legacy_ledger_duplicate_check_uses_readonly_encrypted_snapshot(self):
        key = Fernet.generate_key()
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE links (digest TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)"
        )
        digest = "a" * 64
        db.execute(
            "INSERT INTO links VALUES (?, ?, ?, ?)",
            (digest, "0123456789", "paid", 1800000000),
        )
        encrypted = Fernet(key).encrypt(db.serialize())
        db.close()
        with patch.dict(os.environ, {"PAYPAY_ENCRYPTION_KEY": key.decode("ascii")}):
            with patch.object(github_runtime_store, "enabled", return_value=True):
                with patch.object(github_runtime_store, "read_blob_readonly", return_value=encrypted) as read:
                    self.assertTrue(paypay_gateway._legacy_ledger_contains_digest(digest))
                    self.assertFalse(paypay_gateway._legacy_ledger_contains_digest("b" * 64))
                    self.assertEqual(read.call_count, 2)

    def test_legacy_ledger_duplicate_check_fails_closed_when_snapshot_missing(self):
        with patch.object(github_runtime_store, "enabled", return_value=True):
            with patch.object(github_runtime_store, "read_blob_readonly", return_value=None):
                with self.assertRaises(paypay_gateway.PayPayError) as error:
                    paypay_gateway._legacy_ledger_contains_digest("a" * 64)
        self.assertEqual(error.exception.code, "runtime_store")

    def test_legacy_ledger_duplicate_check_fails_closed_when_schema_missing(self):
        key = Fernet.generate_key()
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE unrelated (value TEXT)")
        encrypted = Fernet(key).encrypt(db.serialize())
        db.close()
        with patch.dict(os.environ, {"PAYPAY_ENCRYPTION_KEY": key.decode("ascii")}):
            with patch.object(github_runtime_store, "enabled", return_value=True):
                with patch.object(github_runtime_store, "read_blob_readonly", return_value=encrypted):
                    with self.assertRaises(paypay_gateway.PayPayError) as error:
                        paypay_gateway._legacy_ledger_contains_digest("a" * 64)
        self.assertEqual(error.exception.code, "runtime_store")

    def test_legacy_otp_state_consume_is_single_use(self):
        original = dict(paypay_gateway._pending_otp)
        try:
            paypay_gateway._pending_otp_clear()
            paypay_gateway._pending_otp_save({"otp_reference_id": "single-use"})
            first = paypay_gateway._pending_otp_consume()
            second = paypay_gateway._pending_otp_consume()
            self.assertEqual(first["otp_reference_id"], "single-use")
            self.assertEqual(second, {})
            self.assertFalse(paypay_gateway.otp_pending())
        finally:
            paypay_gateway._pending_otp.clear()
            paypay_gateway._pending_otp.update(original)

    def test_legacy_otp_state_helpers_keep_existing_in_memory_behavior(self):
        original = dict(paypay_gateway._pending_otp)
        try:
            paypay_gateway._pending_otp_clear()
            self.assertFalse(paypay_gateway.otp_pending())
            paypay_gateway._pending_otp_save({"otp_reference_id": "test-ref"})
            self.assertTrue(paypay_gateway.otp_pending())
            self.assertEqual(paypay_gateway._pending_otp_load()["otp_reference_id"], "test-ref")
            paypay_gateway._pending_otp_clear()
            self.assertFalse(paypay_gateway.otp_pending())
        finally:
            paypay_gateway._pending_otp.clear()
            paypay_gateway._pending_otp.update(original)


if __name__ == "__main__":
    unittest.main()
