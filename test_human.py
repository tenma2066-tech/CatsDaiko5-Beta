"""購入のときの人間確認（Cloudflare Turnstile）。鍵が無ければ何もしない・あればサーバーで確かめる。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as A
import human
import paypay_gateway as G

KEYS = {"TURNSTILE_SITE_KEY": "site-key-123", "TURNSTILE_SECRET_KEY": "secret-key-456"}
LINK = "https://pay.paypay.ne.jp/AbCdEfGhIjKlMnOp"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.object(A, "ADMIN_PASSWORD", "pass-1234"),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True), patch.dict(A._fail, {}, clear=True),
            patch.object(G, "history_upsert"), patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True), patch.object(A, "_reconcile_orders"),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def order(self):
        return self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]

    def pay(self, oid, **body):
        """支払いを送る。人間確認を通ったあとの処理（PayPay）は、ここでは「リンクが違う」で止める。"""
        with patch.object(G, "receive_payment", side_effect=G.PayPayError("link_invalid", "x")):
            return self.c.post(f"/api/orders/{oid}/pay", json=dict({"link": LINK}, **body))


class Disabled(Base):
    def test_nothing_changes_without_keys(self):
        with patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "", "TURNSTILE_SECRET_KEY": ""}), patch.object(human, "verify") as verify:
            self.assertFalse(human.enabled())
            for path in ("/nyanko", "/tsum", "/puni", "/ticket", "/box"):
                r = self.c.get(path)
                html = r.get_data(as_text=True)
                self.assertNotIn("humanBox", html, path)
                self.assertNotIn("challenges.cloudflare.com", html + r.headers["Content-Security-Policy"], path)
            self.assertNotEqual(self.pay(self.order()).get_json().get("code"), "human_required")
            verify.assert_not_called()

    def test_one_key_alone_is_not_enough(self):
        with patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "x", "TURNSTILE_SECRET_KEY": ""}):
            self.assertFalse(human.enabled())
            self.assertEqual(human.site_key(), "")


class Enabled(Base):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, KEYS)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_pages_show_the_widget_and_csp_allows_only_cloudflare(self):
        for path in ("/nyanko", "/tsum", "/puni", "/ticket", "/support", "/source", "/box"):
            r = self.c.get(path)
            html, csp = r.get_data(as_text=True), r.headers["Content-Security-Policy"]
            self.assertIn('id="humanBox" class="humanbox" data-sitekey="site-key-123"', html, path)
            self.assertIn("https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit", html, path)
            self.assertIn("/static/human.js", html, path)
            self.assertIn("frame-src https://challenges.cloudflare.com", csp)
            self.assertRegex(csp, r"script-src 'self' 'nonce-[\w-]+' https://challenges\.cloudflare\.com;")
            self.assertNotIn("secret-key-456", html)                       # サーバーだけが持つ鍵は、画面に出さない
        self.assertNotIn("human.js", self.c.get("/admin").get_data(as_text=True))

    def test_payment_needs_a_verified_token(self):
        oid = self.order()
        with patch.object(human, "verify", return_value=False) as verify:
            r = self.pay(oid)
            self.assertEqual((r.status_code, r.get_json()["code"]), (400, "human_required"))
            self.assertEqual(A._orders[oid]["state"], "unpaid")              # 注文には触らない
            self.assertEqual(self.pay(oid, human="fake").get_json()["code"], "human_required")
            self.assertEqual(verify.call_args.args[0], "fake")
        with patch.object(human, "verify", return_value=True) as verify:
            self.assertNotEqual(self.pay(oid, human="good").get_json().get("code"), "human_required")
            self.assertEqual(verify.call_count, 1)
            self.assertNotEqual(self.pay(oid).get_json().get("code"), "human_required")   # しばらくは聞き直さない
            self.assertEqual(verify.call_count, 1)

    def test_asks_again_after_a_while(self):
        oid = self.order()
        with patch.object(human, "verify", return_value=True):
            self.pay(oid, human="good")
        with self.c.session_transaction() as s:
            s["human_at"] = time.time() - A.HUMAN_TTL - 5
        with patch.object(human, "verify", return_value=False):
            self.assertEqual(self.pay(oid).get_json()["code"], "human_required")

    def test_another_browser_is_asked(self):
        oid = self.order()
        with patch.object(human, "verify", return_value=True):
            self.pay(oid, human="good")
        with patch.object(human, "verify", return_value=False):
            other = A.app.test_client()
            oid2 = other.post("/run", json={"items": ["XP"]}).get_json()["order"]
            with patch.object(G, "receive_payment", side_effect=G.PayPayError("link_invalid", "x")):
                self.assertEqual(other.post(f"/api/orders/{oid2}/pay", json={"link": LINK}).get_json()["code"], "human_required")

    def test_cloudflare_down_does_not_stop_sales(self):
        oid = self.order()
        with patch.object(human, "verify", return_value=None):
            self.assertNotEqual(self.pay(oid).get_json().get("code"), "human_required")
        with self.c.session_transaction() as s:
            self.assertNotIn("human_at", s)                                  # 確認できたわけではないので、覚えない

    def test_hold_recheck_is_not_asked(self):
        oid = self.order()
        with patch.object(human, "verify", return_value=False):
            A._orders[oid].update(state="hold", link_code="AbCdEfGhIjKlMnOp")
            with patch.object(G, "receive_payment", side_effect=G.PayPayError("temporary_hold", "保留")):
                self.assertNotEqual(self.c.post(f"/api/orders/{oid}/pay", json={}).get_json().get("code"), "human_required")

    def test_admin_is_not_asked(self):
        with self.c.session_transaction() as s:
            s["admin"] = True
            s["admin_at"] = s["admin_seen"] = time.time()
        with patch.object(human, "verify", return_value=False) as verify:
            self.assertTrue(A.app.test_request_context() is not None)
            with self.c:
                self.c.get("/api/state")
                self.assertTrue(A._human_ok(""))
            verify.assert_not_called()

    def test_feedback_box_is_checked_too(self):
        body = {"kind": "opinion", "text": "ツムツムの結果画面に、かかった時間も出るとうれしいです。"}
        with patch.object(human, "verify", return_value=False):
            self.assertEqual(self.c.post("/api/box", json=body).get_json()["code"], "human_required")
            self.assertEqual(G.feedback_list(), [])
        with patch.object(human, "verify", return_value=True):
            self.assertTrue(self.c.post("/api/box", json=dict(body, human="good")).get_json()["ok"])
        self.assertEqual(len(G.feedback_list()), 1)


class Verify(unittest.TestCase):
    """Cloudflare への問い合わせ（通信は差し替える）。"""

    def ask(self, reply=None, error=None, token="tok"):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return reply
        seen = {}

        def fake(req, timeout=None):
            seen.update(url=req.full_url, body=req.data.decode(), timeout=timeout)
            if error:
                raise error
            return Resp()
        with patch.dict(os.environ, KEYS), patch("urllib.request.urlopen", fake):
            return human.verify(token, "203.0.113.9"), seen

    def test_success_and_what_is_sent(self):
        ok, seen = self.ask(b'{"success": true}')
        self.assertIs(ok, True)
        self.assertEqual(seen["url"], "https://challenges.cloudflare.com/turnstile/v0/siteverify")
        self.assertEqual(seen["body"], "secret=secret-key-456&response=tok&remoteip=203.0.113.9")
        self.assertEqual(seen["timeout"], human.TIMEOUT)

    def test_rejections(self):
        self.assertIs(self.ask(b'{"success": false, "error-codes": ["timeout-or-duplicate"]}')[0], False)
        self.assertIs(self.ask(b'{"success": false, "error-codes": ["invalid-input-response"]}')[0], False)
        self.assertIs(self.ask(b'{"success": true}', token="")[0], False)          # 印が無い → 聞くまでもなく不可
        self.assertIs(self.ask(b'{"success": true}', token="x" * 5000)[0], False)

    def test_unreachable_or_misconfigured_is_unknown(self):
        self.assertIsNone(self.ask(error=OSError("down"))[0])
        self.assertIsNone(self.ask(b"not json")[0])
        self.assertIsNone(self.ask(b'{"success": false, "error-codes": ["internal-error"]}')[0])
        self.assertIsNone(self.ask(b'{"success": false, "error-codes": ["invalid-input-secret"]}')[0])


if __name__ == "__main__":
    unittest.main()
