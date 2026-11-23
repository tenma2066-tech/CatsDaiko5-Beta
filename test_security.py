"""セキュリティの守り（2026-11 の精査 P0）のテスト。"""
import io
import os
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import app as A

CADDY = {"REMOTE_ADDR": "172.18.0.3"}     # 本番では Caddy（同じサーバーの中）から来る


class Security(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(A, "ADMIN_PASSWORD", "pass-1234"), patch.dict(A._fail, {}, clear=True),
                        patch.dict(os.environ, {"PAYPAY_DATA_DIR": self.tmp.name})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # ---- /api/drain（デプロイ用）は外から使えない
    def test_drain_only_from_inside(self):
        c = A.app.test_client()
        r = c.post("/api/drain", data={"token": "pass-1234"}, environ_base=CADDY, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 404)                       # 外から（Caddy 経由）は存在しない扱い
        r = c.post("/api/drain", data={"token": "pass-1234"}, headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(r.status_code, 404)                       # 転送の印があれば外から
        for _ in range(5):
            self.assertEqual(c.post("/api/drain", data={"token": "wrong"}).status_code, 403)
        self.assertEqual(c.post("/api/drain", data={"token": "pass-1234"}).status_code, 429)   # 失敗が続いたら止める
        A._fail.clear()
        r = c.post("/api/drain", data={"token": "pass-1234", "off": "1"})
        self.assertEqual(r.status_code, 200)                       # サーバーの中からの正しい呼び出しは今まで通り

    # ---- 管理者ログインの失敗回数は、本当の接続元ごとに数える
    def _unlock(self, ip, pw):
        c = A.app.test_client()
        c.get("/admin", environ_base=CADDY, headers={"X-Forwarded-For": ip})
        with c.session_transaction() as s:
            csrf = s["admin_csrf"]
        c.post("/admin", data={"csrf": csrf, "action": "unlock", "passcode": pw}, environ_base=CADDY, headers={"X-Forwarded-For": ip})
        with c.session_transaction() as s:
            return c, bool(s.get("admin"))

    def test_admin_lockout_is_per_client(self):
        for _ in range(5):
            self._unlock("203.0.113.1", "bad")
        self.assertFalse(self._unlock("203.0.113.1", "pass-1234")[1])    # 間違え続けた人は10分待ち
        c, ok = self._unlock("198.51.100.7", "pass-1234")
        self.assertTrue(ok)                                        # ほかの人（本物の管理者）は入れる
        with c.session_transaction() as s:
            self.assertIn("admin_at", s)

    def test_admin_lockout_global_limit(self):
        for i in range(30):
            self._unlock(f"203.0.113.{i + 1}", "bad")            # IP を変えながらの総当たり
        self.assertFalse(self._unlock("198.51.100.7", "pass-1234")[1])

    def test_admin_session_expires(self):
        c = A.app.test_client()
        with c.session_transaction() as s:
            s["admin"] = True
            s["admin_at"] = time.time() - A.ADMIN_SESSION_SEC - 5
        self.assertNotIn("管理者モードを終了", c.get("/admin").get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertNotIn("admin", s)
            s["admin"] = True                                      # 以前からのログイン（時刻なし）は、いまから数える
        self.assertIn("管理者モードを終了", c.get("/admin").get_data(as_text=True))

    # ---- 基本の守りのヘッダー
    def test_security_headers(self):
        c = A.app.test_client()
        r = c.get("/health")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Strict-Transport-Security", r.headers)
        r = c.get("/health", environ_base=CADDY, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("max-age=", r.headers["Strict-Transport-Security"])

    # ---- 管理画面からのバックアップ
    def test_backup_download(self):
        db = sqlite3.connect(os.path.join(self.tmp.name, "payments.sqlite3"))
        db.execute("CREATE TABLE t(x)"); db.execute("INSERT INTO t VALUES(7)"); db.commit()
        with open(os.path.join(self.tmp.name, "orders.enc"), "w") as f:
            f.write("enc")
        c = A.app.test_client()
        c.get("/admin")
        with c.session_transaction() as s:
            csrf = s["admin_csrf"]
        self.assertEqual(c.post("/admin", data={"csrf": csrf, "action": "backup_download"}).status_code, 403)   # 管理者だけ
        with c.session_transaction() as s:
            s["admin"] = True
            s["admin_at"] = time.time()
        r = c.post("/admin", data={"csrf": csrf, "action": "backup_download"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        tf = tarfile.open(fileobj=io.BytesIO(r.data), mode="r:gz")
        self.assertEqual(sorted(tf.getnames()), ["orders.enc", "payments.sqlite3"])
        snap = os.path.join(self.tmp.name, "snap.sqlite3")
        with open(snap, "wb") as f:
            f.write(tf.extractfile("payments.sqlite3").read())
        self.assertEqual(sqlite3.connect(snap).execute("SELECT x FROM t").fetchone()[0], 7)
        db.close()


if __name__ == "__main__":
    unittest.main()
