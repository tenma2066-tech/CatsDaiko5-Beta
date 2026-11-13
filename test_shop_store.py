import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet


class ShopStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHOP_DB_PATH"] = str(Path(self.tmp.name) / "orders.sqlite3")
        os.environ["PAYPAY_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        os.environ["SHOP_CATALOG_JSON"] = json.dumps([
            {"id": "svc-a", "title": "テスト商品", "price_yen": 1234, "description": "説明"}
        ], ensure_ascii=False)
        import shop_store
        self.store = importlib.reload(shop_store)

    def tearDown(self):
        self.tmp.cleanup()
        for key in ("SHOP_DB_PATH", "PAYPAY_ENCRYPTION_KEY", "SHOP_CATALOG_JSON"):
            os.environ.pop(key, None)

    def test_order_uses_server_catalog_and_hides_credentials_from_public_view(self):
        order = self.store.create_order("svc-a", "transfer-secret", "9876")
        self.assertEqual(order["amount"], 1234)
        public = self.store.public_order(order["id"], order["access_token"])
        self.assertEqual(public["status"], "awaiting_payment")
        self.assertNotIn("transfer-secret", json.dumps(public))
        self.assertNotIn("9876", json.dumps(public))
        admin = self.store.get_order_for_admin(order["id"])
        self.assertEqual(admin["credentials"]["transfer_code"], "transfer-secret")
        self.assertEqual(admin["credentials"]["pin"], "9876")

    def test_client_cannot_choose_price(self):
        order = self.store.create_order("svc-a", "abc", "9999")
        self.assertEqual(order["amount"], 1234)

    def test_unknown_product_rejected(self):
        with self.assertRaises(self.store.ShopError):
            self.store.create_order("not-for-sale", "abc", "9876")

    def test_order_state_machine(self):
        order = self.store.create_order("svc-a", "abc", "9876")
        self.store.set_status(order["id"], "paid", payment_digest="digest-1")
        self.store.set_status(order["id"], "fulfilling")
        self.store.set_status(order["id"], "completed")
        with self.assertRaises(self.store.ShopError):
            self.store.set_status(order["id"], "paid")

    def test_wrong_access_token_cannot_read_order(self):
        order = self.store.create_order("svc-a", "abc", "9876")
        self.assertIsNone(self.store.public_order(order["id"], "wrong-token"))


if __name__ == "__main__":
    unittest.main()
