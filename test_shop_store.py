import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from cryptography.fernet import Fernet


class ShopStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHOP_DB_PATH"] = str(Path(self.tmp.name) / "orders.sqlite3")
        os.environ["SHOP_STORAGE_BACKEND"] = "legacy"
        os.environ["PAYPAY_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        os.environ["SHOP_CATALOG_JSON"] = json.dumps([
            {"id": "svc-a", "title": "テスト商品", "price_yen": 1234, "description": "説明"}
        ], ensure_ascii=False)
        import shop_store
        self.store = importlib.reload(shop_store)

    def tearDown(self):
        self.tmp.cleanup()
        for key in ("SHOP_DB_PATH", "PAYPAY_ENCRYPTION_KEY", "SHOP_CATALOG_JSON", "SHOP_STORAGE_BACKEND"):
            os.environ.pop(key, None)

    def test_firestore_selection_fails_closed_without_falling_back(self):
        os.environ["SHOP_STORAGE_BACKEND"] = "firestore"
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("svc-a")
        self.assertFalse((Path(self.tmp.name) / "orders.sqlite3").exists())

    def test_unknown_storage_backend_is_rejected(self):
        os.environ["SHOP_STORAGE_BACKEND"] = "typo"
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("svc-a")

    def test_order_uses_server_catalog_and_hides_credentials_from_public_view(self):
        order = self.store.create_order("svc-a")
        self.assertEqual(order["amount"], 1234)
        public = self.store.public_order(order["id"], order["access_token"])
        self.assertEqual(public["status"], "awaiting_payment")
        self.assertFalse(public["credentials_submitted"])
        self.assertNotIn("transfer-secret", json.dumps(public))
        with self.assertRaises(self.store.ShopError):
            self.store.save_credentials(order["id"], order["access_token"], "transfer-secret", "9876")
        self.assertTrue(self.store.claim_payment(order["id"]))
        self.store.complete_payment(order["id"], "digest-1")
        self.store.save_credentials(order["id"], order["access_token"], "transfer-secret", "9876")
        public_after = self.store.public_order(order["id"], order["access_token"])
        self.assertTrue(public_after["credentials_submitted"])
        self.assertNotIn("transfer-secret", json.dumps(public_after))
        admin = self.store.get_order_for_admin(order["id"])
        self.assertEqual(admin["credentials"]["transfer_code"], "transfer-secret")
        self.assertEqual(admin["credentials"]["pin"], "9876")

    def test_price_is_always_taken_from_server_catalog(self):
        order = self.store.create_order("svc-a", quantity=2)
        self.assertEqual(order["unit_price_yen"], 1234)
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["amount"], 2468)

    def test_unknown_product_rejected(self):
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("not-for-sale")

    def test_order_state_machine_requires_credentials_before_fulfillment(self):
        order = self.store.create_order("svc-a")
        self.assertTrue(self.store.claim_payment(order["id"]))
        self.store.complete_payment(order["id"], "digest-2")
        with self.assertRaises(self.store.ShopError):
            self.store.set_status(order["id"], "fulfilling")
        self.store.save_credentials(order["id"], order["access_token"], "transfer", "9876")
        self.store.set_status(order["id"], "fulfilling")
        self.store.set_status(order["id"], "completed")
        with self.assertRaises(self.store.ShopError):
            self.store.set_status(order["id"], "paid")

    def test_default_catalog_has_requested_per_unit_prices(self):
        configured = os.environ.pop("SHOP_CATALOG_JSON", None)
        try:
            items = {item["id"]: item for item in self.store.catalog()}
            self.assertEqual(items["character-specific"]["unit_price_yen"], 50)
            self.assertEqual(items["other-service"]["unit_price_yen"], 25)
        finally:
            if configured is not None:
                os.environ["SHOP_CATALOG_JSON"] = configured

    def test_payment_claim_is_atomic_and_only_one_request_wins(self):
        order = self.store.create_order("svc-a")
        self.assertTrue(self.store.claim_payment(order["id"]))
        self.assertFalse(self.store.claim_payment(order["id"]))
        self.assertEqual(self.store.public_order(order["id"], order["access_token"])["status"],
                         "payment_processing")
        self.assertTrue(self.store.release_payment(order["id"]))
        self.assertTrue(self.store.claim_payment(order["id"]))
        self.store.complete_payment(order["id"], "digest-race")
        self.assertFalse(self.store.claim_payment(order["id"]))

    def test_concurrent_payment_claims_allow_exactly_one_winner(self):
        order = self.store.create_order("svc-a")
        barrier = threading.Barrier(3)
        results = []
        def claim():
            barrier.wait()
            results.append(self.store.claim_payment(order["id"]))
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    def test_quantity_limits_are_enforced(self):
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("svc-a", quantity=0)
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("svc-a", quantity=101)
    def test_wrong_access_token_cannot_read_order(self):
        order = self.store.create_order("svc-a")
        self.assertIsNone(self.store.public_order(order["id"], "wrong-token"))


    def test_original_operation_order_encrypts_payload_and_uses_fixed_price(self):
        operation = {
            "items": ["XP"], "subs": [], "others": [], "qty": {},
            "chars": "", "cat_settings": {}, "safe": True,
            "dry": False, "clone": False, "integrity_only": False,
        }
        order = self.store.create_operation_order(operation, 4)
        self.assertEqual(order["unit_price_yen"], 25)
        self.assertEqual(order["quantity"], 4)
        self.assertEqual(order["amount"], 100)
        public = self.store.public_order(order["id"], order["access_token"])
        self.assertNotIn("operation", json.dumps(public))
        self.assertTrue(self.store.claim_payment(order["id"]))
        self.store.complete_payment(order["id"], "operation-digest")
        self.store.save_credentials(order["id"], order["access_token"], "transfer-code", "1234")
        saved = self.store.operation_for_execution(order["id"], order["access_token"])
        self.assertEqual(saved["items"], ["XP"])
        self.assertEqual(saved["transfer"], "transfer-code")
        self.assertEqual(saved["confirm"], "1234")
        self.assertTrue(self.store.claim_execution(order["id"], order["access_token"]))
        with self.assertRaises(self.store.ShopError):
            self.store.claim_execution(order["id"], order["access_token"])
        self.store.bind_job(order["id"], "job-123")
        self.assertEqual(
            self.store.public_order(order["id"], order["access_token"])["job_id"],
            "job-123",
        )
        self.store.set_status(order["id"], "fulfilling")
        self.store.set_status(order["id"], "completed")

    def test_character_specific_quantity_and_price_are_derived_from_operation(self):
        # A forged client quantity of 1 cannot reduce three selected characters to 50 yen.
        order = self.store.create_operation_order({"chars": "12,13,14"}, 1)
        self.assertEqual(order["unit_price_yen"], 50)
        self.assertEqual(order["quantity"], 3)
        self.assertEqual(order["amount"], 150)

    def test_operation_order_rejects_invalid_quantity_and_character_input(self):
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({}, 0)
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({}, 101)
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({"chars": "not-a-number"}, 1)
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({"chars": "1-101"}, 1)

    def test_operation_order_requires_valid_order_token(self):
        order = self.store.create_operation_order({"items": ["XP"]}, 1)
        self.assertIsNone(
            self.store.operation_for_execution(order["id"], "wrong-token")
        )

    def test_public_operation_flags_must_be_real_booleans(self):
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({"clone": "false"}, 1)
        with self.assertRaises(self.store.ShopError):
            self.store.create_operation_order({"clone": True, "integrity_only": True}, 1)

    def test_new_account_order_accepts_both_credentials_blank(self):
        order = self.store.create_operation_order(
            {"items": ["XP"], "clone": False, "integrity_only": False}, 1
        )
        self.store.claim_payment(order["id"])
        self.store.complete_payment(order["id"], "new-account-digest")
        self.store.save_credentials(order["id"], order["access_token"], "", "")
        operation = self.store.operation_for_execution(order["id"], order["access_token"])
        self.assertEqual(operation["transfer"], "")
        self.assertEqual(operation["confirm"], "")

    def test_existing_save_modes_require_both_credentials(self):
        order = self.store.create_operation_order({"clone": True}, 1)
        self.store.claim_payment(order["id"])
        self.store.complete_payment(order["id"], "clone-digest")
        self.store.save_credentials(order["id"], order["access_token"], "", "")
        with self.assertRaises(self.store.ShopError):
            self.store.operation_for_execution(order["id"], order["access_token"])


if __name__ == "__main__":
    unittest.main()
