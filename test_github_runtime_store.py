import base64
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import github_runtime_store as store


class GitHubRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "RUNTIME_GITHUB_REPO": "owner/private-beta",
            "RUNTIME_GITHUB_TOKEN": "test-token",
            "RUNTIME_GITHUB_BRANCH": "runtime-data",
            "RUNTIME_GITHUB_PREFIX": "runtime",
            "PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        })
        self.env.start()
        self.remote = {}
        self.sha = {}
        self.get = store._read_remote
        self.request = store._request

        def read(name):
            return self.remote.get(name), self.sha.get(name)

        def request(method, url, token, payload=None):
            if method == "PUT":
                path = url.split("/contents/", 1)[1]
                name = path.removeprefix("runtime/")
                self.remote[name] = base64.b64decode(payload["content"])
                self.sha[name] = "new-sha"
                return {"content": {"sha": "new-sha"}}
            raise AssertionError("Unexpected request")

        store._read_remote = read
        store._request = request

    def tearDown(self):
        store._read_remote = self.get
        store._request = self.request
        self.env.stop()

    def test_sqlite_snapshot_is_encrypted_and_survives_reopen(self):
        with store.sqlite_database("orders.sqlite3", "/tmp/not-used-orders.sqlite3") as db:
            db.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, note TEXT)")
            db.execute("INSERT INTO orders VALUES (?, ?)", ("A1", "private customer data"))
        saved = self.remote["orders.sqlite3"]
        self.assertNotIn(b"private customer data", saved)
        with store.sqlite_database("orders.sqlite3", "/tmp/not-used-orders.sqlite3") as db:
            row = db.execute("SELECT note FROM orders WHERE id='A1'").fetchone()
            self.assertEqual(row["note"], "private customer data")

    def test_blob_round_trip(self):
        store.write_blob("paypay/account.enc", b"already-encrypted-account")
        self.assertEqual(store.read_blob("paypay/account.enc"), b"already-encrypted-account")

    def test_remote_write_is_not_silently_overwritten_on_conflict(self):
        def conflict(method, url, token, payload=None):
            raise store.RuntimeStoreError("conflict")
        store._request = conflict
        with self.assertRaises(store.RuntimeStoreError):
            store.write_blob("paypay/account.enc", b"data")


if __name__ == "__main__":
    unittest.main()
