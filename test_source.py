"""ソースコード購入（/source）と、販売用 zip（source_pack.py）のテスト。PayPay通信はモック。"""
import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as A
import paypay_gateway as G
import source_pack as SP


class Pack(unittest.TestCase):
    """zip の中身：必要なものが入り、持ち主の情報・データ・鍵が入っていないこと。"""

    @classmethod
    def setUpClass(cls):
        cls.blob = SP.build()
        cls.zip = zipfile.ZipFile(io.BytesIO(cls.blob))
        cls.names = [n[len(SP.PACKAGE_DIR) + 1:] for n in cls.zip.namelist()]

    def read(self, rel: str) -> str:
        return self.zip.read(f"{SP.PACKAGE_DIR}/{rel}").decode("utf-8")

    def test_contains_what_a_buyer_needs(self):
        for rel in ("app.py", "site_config.py", "pricing.py", "paypay_gateway.py", "requirements.txt", "Dockerfile",
                    "templates/main.html", "templates/extra.html", "static/shop.js", "deploy/vps/docker-compose.yml",
                    ".github/workflows/vps-deploy.yml", "docs/SETUP.md", "README.md", "LICENSE.md", "test_order_flow.py"):
            self.assertIn(rel, self.names, rel)
        self.assertTrue(all(n.startswith(SP.PACKAGE_DIR + "/") for n in self.zip.namelist()))

    def test_excludes_data_secrets_and_unrelated_projects(self):
        for n in self.names:
            self.assertFalse(n.startswith(("instance/", "xbot/", "exchange/", ".git/")), n)
            self.assertFalse(n.endswith((".sqlite3", ".log", ".pyc", ".env")), n)
            self.assertNotIn("__pycache__", n)
        for rel in ("deploy/vps/known_hosts", "CLAUDE.md", "AGENTS.md", "docs/OLD_README.md"):
            self.assertNotIn(rel, self.names)

    def test_owner_identity_is_scrubbed(self):
        for n in self.names:
            if not SP._is_text(n):
                continue
            low = self.read(n).lower()
            for word in SP.FORBIDDEN:
                self.assertNotIn(word, low, f"{n} に {word} が残っています")
        cfg = self.read("site_config.py")
        self.assertIn('_env("SHOP_X_ID", "")', cfg)                 # 問い合わせ先は空（買った人が設定する）
        self.assertRegex(cfg, r'"GOOGLE_SITE_VERIFICATION",\s*""\)')
        self.assertIn('_env("REVIEW_POST_ID", "")', cfg)
        packer = self.read("source_pack.py")                         # 置き換え表そのものも持ち主の値を含まない
        self.assertIn("REPLACE = ()", packer)
        self.assertIn("FORBIDDEN = ()", packer)

    def test_packed_code_still_compiles(self):
        for n in self.names:
            if n.endswith(".py") and not n.startswith(("puni_engine/", "tsum_engine/")):
                compile(self.read(n), n, "exec")

    def test_verify_rejects_leftovers(self):
        # 配布された側（置き換え表が空）でも通るよう、検査する語はその場で指定する
        with patch.object(SP, "FORBIDDEN", ("secret-owner-word",)):
            with self.assertRaises(ValueError):
                SP.verify({"x.md": b"contact: Secret-Owner-Word"})
            SP.verify({"x.md": b"contact: nobody", "logo.png": b"secret-owner-word"})   # 画像などは検査の対象外

    def test_build_is_deterministic_and_cached(self):
        self.assertEqual(SP.build(), self.blob)
        self.assertIs(SP.cached(), SP.cached())
        info = SP.summary()
        self.assertEqual(info["files"], len(self.names))
        self.assertGreater(info["lines"], 5000)


class Purchase(unittest.TestCase):
    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True),
            patch.object(G, "history_upsert"), patch.object(G, "mark_used"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def buy(self, c=None):
        c = c or self.c
        j = c.post("/api/source/order", json={}).get_json()
        with patch.object(G, "receive_payment", AsyncMock(return_value=None)):
            p = c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        return j, p

    def test_page_and_price(self):
        page = self.c.get("/source").get_data(as_text=True)
        self.assertIn("ソースコード購入", page)
        self.assertIn(f"{A.PRICES['source_code']:,}円", page)
        self.assertIn("再配布・転売・公開はできません", page)
        self.assertIn('href="/source"', self.c.get("/").get_data(as_text=True))
        self.assertEqual(A.PRICES["source_code"], 5000)

    def test_download_only_after_payment_and_only_for_the_buyer(self):
        j = self.c.post("/api/source/order", json={}).get_json()
        self.assertEqual(j["amount"], A.PRICES["source_code"])
        self.assertEqual(self.c.get(f"/api/source/download/{j['order']}").status_code, 404)        # 未払い
        with patch.object(G, "receive_payment", AsyncMock(return_value=None)):
            p = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        self.assertEqual(p.status_code, 200)
        self.assertTrue(p.get_json()["result"]["download"])
        r = self.c.get(f"/api/source/download/{j['order']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn(f"{SP.PACKAGE_DIR}/docs/SETUP.md", zipfile.ZipFile(io.BytesIO(r.data)).namelist())
        other = A.app.test_client()
        self.assertEqual(other.get(f"/api/source/download/{j['order']}").status_code, 404)        # 他人
        self.assertEqual(other.get("/api/source/download/nope").status_code, 404)
        # 注文番号で呼び戻せば、別の端末でもダウンロードできる
        self.assertEqual(other.post(f"/api/orders/{j['order']}/claim").status_code, 200)
        self.assertEqual(other.get(f"/api/source/download/{j['order']}").status_code, 200)

    def test_kept_30_days_and_no_coupons(self):
        j, p = self.buy()
        o = A._orders[j["order"]]
        self.assertEqual((o["state"], o["keep"]), ("done", A.SOURCE_KEEP_DAYS * 86400))
        o["created"] = time.time() - 10 * 86400            # 10日後：まだ残る（ふつうの完了注文は3日）
        A._gc_orders()
        self.assertIn(j["order"], A._orders)
        o["created"] = time.time() - 31 * 86400
        A._gc_orders()
        self.assertNotIn(j["order"], A._orders)
        free = G.coupon_issue_free(30)                     # 無料クーポンを付けて送っても、定価のまま
        k = self.c.post("/api/source/order", json={"coupon": free}).get_json()
        self.assertEqual(k["amount"], A.PRICES["source_code"])

    def test_other_ticket_orders_cannot_download(self):
        t = self.c.post("/api/ticket/order", json={"plan": "once"}).get_json()
        with patch.object(G, "receive_payment", AsyncMock(return_value=None)):
            self.c.post(f"/api/orders/{t['order']}/pay", json={"link": self.LINK})
        self.assertEqual(self.c.get(f"/api/source/download/{t['order']}").status_code, 404)

    def test_pack_failure_is_reported_not_500(self):
        j, _ = self.buy()
        with patch.object(SP, "cached", side_effect=ValueError("leftover")), self.assertLogs("orders", level="ERROR"):
            r = self.c.get(f"/api/source/download/{j['order']}")
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "pack_failed"))


if __name__ == "__main__":
    unittest.main()
