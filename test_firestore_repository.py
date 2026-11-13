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

    def test_credentials_and_outbox_are_created_atomically(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        execution_id = "exec_" + secrets.token_hex(8)
        result = workflow.submit_credentials_and_enqueue(
            self.order_id, self.access_hash, b"encrypted-transfer", execution_id
        )
        self.assertEqual(result["execution_id"], execution_id)
        order = self.orders.get_order(self.order_id)
        self.assertTrue(order["credentials_submitted"])
        self.assertEqual(order["active_execution_id"], execution_id)
        self.assertEqual(order["job_id"], "queued")
        self.assertEqual(workflow.get_execution(execution_id)["state"], "queued")
        self.assertEqual(workflow.get_outbox_item(execution_id)["state"], "pending")
        with self.assertRaises(WorkflowStateError):
            workflow.submit_credentials_and_enqueue(
                self.order_id, self.access_hash, b"replacement", "exec_" + secrets.token_hex(8)
            )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()

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
        # A second enqueue with the same order is rejected by the order guard.
        with self.assertRaises(WorkflowStateError):
            workflow.enqueue_execution(self.order_id, "exec_" + secrets.token_hex(8))

        # First task delivery wins the launch gate; duplicate deliveries are no-ops.
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
        stored = self.client.collection("admin_otp_sessions").document(session_id).get().to_dict()
        self.assertNotIn("plain-otp", stored["encrypted_state"])
        self.assertEqual(workflow.get_otp_session(session_id), encrypted)
        self.assertEqual(workflow.consume_otp_session(session_id), encrypted)
        self.assertIsNone(workflow.consume_otp_session(session_id))
        workflow.save_otp_session(session_id, "renewed-encrypted-state", time.time() + 300)
        self.assertEqual(workflow.get_otp_session(session_id), "renewed-encrypted-state")
        workflow.clear_otp_session(session_id)
        self.assertIsNone(workflow.get_otp_session(session_id))
        self.client.collection("admin_otp_sessions").document(session_id).delete()

    def test_dispatch_defers_when_all_global_slots_are_occupied(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        for index in range(4):
            self.client.collection("worker_slots").document(f"slot-{index}").set({
                "state": "active", "owner": "other-worker",
            })
        try:
            result = workflow.begin_dispatch(execution_id)
            self.assertEqual(result, {"state": "no_slot"})
            self.assertEqual(
                workflow.get_outbox_item(execution_id)["state"], "task_enqueued"
            )
        finally:
            for index in range(4):
                self.client.collection("worker_slots").document(f"slot-{index}").delete()
            self.client.collection("task_outbox").document(execution_id).delete()
            self.client.collection("executions").document(execution_id).delete()
            self.client.collection("orders").document(self.order_id).update({
                "status": "paid", "job_id": None, "active_execution_id": None,
            })

    def test_reserved_slot_can_be_released_before_worker_claim(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["slot_id"], "slot-0")
        workflow.fail_reserved_execution(
            self.order_id, execution_id, "slot-0", "worker_claim_failed"
        )
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()

    def test_shop_store_uses_firestore_and_persists_order_outbox(self):
        import importlib
        from cryptography.fernet import Fernet

        import shop_store

        previous = {
            key: os.environ.get(key)
            for key in ("SHOP_STORAGE_BACKEND", "PAYPAY_ENCRYPTION_KEY",
                        "SHOP_CATALOG_JSON", "GOOGLE_CLOUD_PROJECT")
        }
        os.environ["SHOP_STORAGE_BACKEND"] = "firestore"
        os.environ["PAYPAY_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ.get(
            "GOOGLE_CLOUD_PROJECT", "demo-catsdaiko-test"
        )
        os.environ.pop("SHOP_CATALOG_JSON", None)
        shop_store = importlib.reload(shop_store)
        created = None
        try:
            operation = {
                "safe": True, "dry": False, "clone": False, "integrity_only": False,
                "items": ["XP"], "qty": {}, "chars": "", "cat_settings": {},
                "subs": [], "others": [],
            }
            created = shop_store.create_operation_order(operation, 1)
            digest = secrets.token_hex(32)
            self.assertTrue(shop_store.claim_payment_attempt(created["id"], digest))
            shop_store.transition_payment_attempt(created["id"], digest, "paid")
            shop_store.save_credentials(
                created["id"], created["access_token"], "", ""
            )
            execution_id = shop_store.enqueue_execution(
                created["id"], created["access_token"]
            )
            self.assertEqual(
                shop_store.execution_progress(
                    created["id"], created["access_token"]
                )["status"],
                "queued",
            )
            self.assertTrue(shop_store.cancel_queued_execution(
                created["id"], created["access_token"]
            ))
            self.assertEqual(
                shop_store.public_order(created["id"], created["access_token"])["status"],
                "cancelled",
            )
            self.client.collection("orders").document(created["id"]).delete()
            self.client.collection("payment_attempts").document(digest).delete()
            self.client.collection("executions").document(execution_id).delete()
            self.client.collection("task_outbox").document(execution_id).delete()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(shop_store)

    def test_reconciler_releases_slot_but_keeps_lock_after_running_job_ends_unknown(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/reconcile-test",
            "execution_name": "job-run-unknown",
        })
        lock_digest = transfer_lock_id("transfer-code-example", b"z" * 32)
        self.assertTrue(workflow.claim_execution(
            self.order_id, execution_id, "job-run-unknown", lock_digest, "slot-0"
        ))
        self.assertTrue(workflow.reconcile_terminal_execution(
            self.order_id, execution_id, owner="job-run-unknown",
            cloud_run_execution_name="job-run-unknown",
        ))
        execution = workflow.get_execution(execution_id)
        self.assertEqual(execution["state"], "review")
        self.assertTrue(execution["outcome_unknown"])
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.assertEqual(
            self.client.collection("transfer_locks").document(lock_digest).get().to_dict()["state"],
            "active",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()
        self.client.collection("transfer_locks").document(lock_digest).delete()

    def test_reconciler_releases_preclaim_reservation_without_account_lock(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/preclaim-test",
            "execution_name": "job-never-claimed",
        })
        self.assertTrue(workflow.reconcile_terminal_execution(
            self.order_id, execution_id, owner=None,
            cloud_run_execution_name="job-never-claimed",
        ))
        execution = workflow.get_execution(execution_id)
        self.assertEqual(execution["state"], "review")
        self.assertFalse(execution["outcome_unknown"])
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()


if __name__ == "__main__":
    unittest.main()
