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

        def read(name, **kwargs):
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
        writes_before = len(self.remote)
        with store.sqlite_database("orders.sqlite3", "/tmp/not-used-orders.sqlite3") as db:
            row = db.execute("SELECT note FROM orders WHERE id='A1'").fetchone()
            self.assertEqual(row["note"], "private customer data")
        self.assertEqual(len(self.remote), writes_before, "read-only access must not create a Git commit")

    def test_legacy_local_database_is_migrated_without_plaintext_upload(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            local = sqlite3.connect(path)
            local.execute("CREATE TABLE sample (value TEXT)")
            local.execute("INSERT INTO sample VALUES ('legacy-private-value')")
            local.commit()
            local.close()
            with store.sqlite_database("legacy.sqlite3", path) as db:
                self.assertEqual(db.execute("SELECT value FROM sample").fetchone()["value"],
                                 "legacy-private-value")
            encrypted = self.remote["legacy.sqlite3"]
            self.assertNotIn(b"legacy-private-value", encrypted)
            self.assertTrue(store._cipher().decrypt(encrypted).startswith(b"SQLite format 3\x00"))

    def test_read_blob_readonly_never_migrates_local_file_to_github(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "legacy.enc"
            local.write_bytes(b"local-encrypted-snapshot")
            result = store.read_blob_readonly("legacy.enc", local)
            self.assertEqual(result, b"local-encrypted-snapshot")
            self.assertNotIn("legacy.enc", self.remote)

    def test_read_blob_readonly_returns_none_without_creating_missing_blob(self):
        self.assertIsNone(store.read_blob_readonly("missing.enc"))
        self.assertNotIn("missing.enc", self.remote)

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
