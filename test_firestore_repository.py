"""Optional Firestore Emulator integration tests for transactional adapters."""
from __future__ import annotations

import os
import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore

from shop_firestore_repository import (
    FirestoreOrderRepository,
    FirestorePaymentRepository,
)


@unittest.skipUnless(
    os.environ.get("FIRESTORE_EMULATOR_HOST"),
    "FIRESTORE_EMULATOR_HOST is not configured",
)
class FirestoreRepositoryEmulatorTests(unittest.TestCase):
    def setUp(self):
        self.client = firestore.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-catsdaiko-test")
        )
        self.orders = FirestoreOrderRepository(self.client)
        self.payments = FirestorePaymentRepository(self.client)
        self.order_id = secrets.token_hex(5).upper()
        self.digest = secrets.token_hex(32)
        self.access_hash = secrets.token_hex(32)
        self.orders.create_order({
            "id": self.order_id,
            "access_hash": self.access_hash,
            "sku": "emulator-test",
            "title": "emulator test",
            "amount": 25,
            "unit_price_yen": 25,
            "quantity": 1,
            "credential_enc": b"encrypted-empty-credentials",
            "operation_enc": None,
            "service_type": "other-service",
            "now": 1_800_000_000.0,
        })

    def tearDown(self):
        self.client.collection("orders").document(self.order_id).delete()
        self.client.collection("payment_attempts").document(self.digest).delete()

    def test_payment_credentials_and_execution_are_transactional(self):
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.assertFalse(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "paid")

        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer-credentials"
        ))
        self.assertFalse(self.orders.save_credentials(
            self.order_id, self.access_hash, b"second-write"
        ))

        barrier = threading.Barrier(3)

        def claim():
            barrier.wait()
            return self.orders.claim_execution(self.order_id, self.access_hash)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            barrier.wait()
            results = [future.result() for future in futures]

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertTrue(self.orders.bind_job(self.order_id, "emulator-job"))
        self.orders.set_status(self.order_id, "fulfilling")
        self.orders.set_status(self.order_id, "completed")
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "completed")

    def test_duplicate_payment_digest_is_rejected(self):
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        second_id = secrets.token_hex(5).upper()
        self.orders.create_order({
            "id": second_id,
            "access_hash": secrets.token_hex(32),
            "sku": "emulator-test",
            "title": "second emulator test",
            "amount": 25,
            "unit_price_yen": 25,
            "quantity": 1,
            "credential_enc": b"encrypted-empty-credentials",
            "operation_enc": None,
            "service_type": "other-service",
            "now": 1_800_000_001.0,
        })
        try:
            self.assertTrue(self.payments.claim_payment(second_id))
            with self.assertRaises(Exception):
                self.payments.complete_payment(second_id, self.digest)
        finally:
            self.client.collection("orders").document(second_id).delete()


if __name__ == "__main__":
    unittest.main()
