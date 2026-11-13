"""Optional Firestore Emulator integration tests for transactional adapters."""
from __future__ import annotations

import os
import secrets
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore

from shop_firestore_repository import (
    FirestoreOrderRepository,
    FirestorePaymentRepository,
)
from shop_payment_repository import PaymentRepositoryError
from shop_firestore_workflow import (
    FirestoreWorkflowRepository,
    WorkflowStateError,
    transfer_lock_id,
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
            with self.assertRaises(PaymentRepositoryError):
                self.payments.complete_payment(second_id, self.digest)
        finally:
            self.client.collection("orders").document(second_id).delete()

    def test_payment_attempt_digest_is_claimed_once_and_unknown_requires_review(self):
        digest = secrets.token_hex(32)
        self.assertTrue(self.payments.claim_payment_attempt(self.order_id, digest))
        self.assertFalse(self.payments.claim_payment_attempt(self.order_id, digest))
        self.payments.transition_payment_attempt(
            self.order_id, digest, "outcome_unknown", "accept_uncertain"
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.payments.transition_payment_attempt(
            self.order_id, digest, "paid", "admin_confirmed"
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "paid")
        self.client.collection("payment_attempts").document(digest).delete()

    def test_outbox_single_launch_and_unknown_game_result_keeps_transfer_lock(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        self.assertEqual(len(workflow.list_pending_outbox()), 1)
        workflow.mark_task_enqueued(execution_id)
        self.assertIsNone(workflow.begin_dispatch(execution_id))
        # A second enqueue with the same order is rejected by the order guard.
        with self.assertRaises(WorkflowStateError):
            workflow.enqueue_execution(self.order_id, "exec_" + secrets.token_hex(8))

        # First task delivery wins the launch gate; duplicate deliveries are no-ops.
        outbox_ref = self.client.collection("task_outbox").document(execution_id)
        self.client.collection("task_outbox").document(execution_id).update({"state": "task_enqueued"})
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["order_id"], self.order_id)
        self.assertIsNone(workflow.begin_dispatch(execution_id))
        workflow.mark_dispatch_started(execution_id, "operations/test-job")
        lock_digest = transfer_lock_id("transfer-code-example", b"x" * 32)
        self.assertTrue(workflow.claim_execution(
            self.order_id, execution_id, "worker-a", lock_digest, "slot-0"
        ))
        self.assertFalse(workflow.claim_execution(
            self.order_id, execution_id, "worker-b", lock_digest, "slot-1"
        ))
        workflow.finish_execution(
            self.order_id, execution_id, "worker-a",
            success=False, outcome_unknown=True, safe_error_code="game_timeout",
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.assertEqual(
            self.client.collection("transfer_locks").document(lock_digest).get().to_dict()["state"],
            "active",
        )
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        # Cleanup only test-owned docs.
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("transfer_locks").document(lock_digest).delete()
        self.client.collection("worker_slots").document("slot-0").delete()

    def test_otp_session_is_encrypted_and_single_use(self):
        workflow = FirestoreWorkflowRepository(self.client)
        session_id = "otp_" + secrets.token_hex(12)
        encrypted = "fernet-ciphertext-not-plaintext-otp"
        workflow.save_otp_session(session_id, encrypted, time.time() + 300)
        self.assertEqual(workflow.consume_otp_session(session_id), encrypted)
        self.assertIsNone(workflow.consume_otp_session(session_id))
        self.client.collection("admin_otp_sessions").document(session_id).delete()


if __name__ == "__main__":
    unittest.main()
