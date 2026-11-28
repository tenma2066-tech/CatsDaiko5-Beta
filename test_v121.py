"""v1.21 で足したもの：意見・質問箱／配る画面の守り（CSP・コメント除去・保存させない）／管理画面の2段階認証／
実績の記入時刻／順番待ちの「やっぱりやめる」／にゃんこの最初の大きな案内をやめたこと。"""
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import admin_2fa
import app as A
import site_config
import paypay_gateway as G

GOOD = "ツムツムの結果画面に、かかった時間も出るとうれしいです。"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.object(A, "ADMIN_PASSWORD", "pass-1234"),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True), patch.dict(A._fail, {}, clear=True),
            patch.object(G, "history_upsert"), patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True), patch.object(A, "_reconcile_orders"),
            patch.dict(os.environ, {"PAYPAY_DATA_DIR": self.tmp.name, "PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode()}),
        ]
        for p in self.patches:
            p.start()
        admin_2fa.cancel()
        admin_2fa._last_counter["value"] = 0
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def csrf(self, c=None):
        c = c or self.c
        c.get("/admin")
        with c.session_transaction() as s:
            return s["admin_csrf"]

    def admin(self, action=None, c=None, **data):
        c = c or self.c
        return c.post("/admin", data=dict(data, csrf=self.csrf(c), action=action))

    def login(self, c=None):
        self.admin("unlock", c, passcode="pass-1234")

    def is_admin(self, c=None):
        return "ログアウト" in (c or self.c).get("/admin").get_data(as_text=True) or 'data-sec="home"' in (c or self.c).get("/admin").get_data(as_text=True)


class FeedbackBox(Base):
    def send(self, **body):
        return self.c.post("/api/box", json=dict({"kind": "opinion", "text": GOOD}, **body))

    def test_page_and_menu(self):
        page = self.c.get("/box").get_data(as_text=True)
        self.assertIn("意見・質問箱", page)
        self.assertIn('href="/box"', self.c.get("/").get_data(as_text=True))
        with patch.object(site_config, "CHINCHIRO_ENABLED", False):   # ミニゲームのカードを除いた、お店のメニューの数
            self.assertEqual(self.c.get("/").get_data(as_text=True).count('class="gamecard"'), 7)   # 7つ目のメニュー
        self.assertIn("/box</loc>", self.c.get("/sitemap.xml").get_data(as_text=True))

    def test_saves_and_only_admin_reads(self):
        self.assertEqual(self.send(contact="@someone_x").get_json(), {"ok": True})
        rows = G.feedback_list()
        self.assertEqual([(r["kind"], r["body"], r["contact"], r["done"]) for r in rows], [("opinion", GOOD, "@someone_x", 0)])
        self.assertNotIn(GOOD, self.c.get("/box").get_data(as_text=True))                       # 公開されない
        self.assertNotIn(GOOD, self.c.get("/admin").get_data(as_text=True))                     # ログイン前の管理画面にも出ない
        self.login()
        page = self.c.get("/admin").get_data(as_text=True)
        self.assertIn(GOOD, page)
        self.assertIn("@someone_x", page)
        self.assertIn('意見箱<span class="badge">1</span>', page)
        for sec in re.findall(r'data-sec="([a-z]+)"', page):                   # どのタブにも、開く中身がある
            self.assertIn(f'id="sec-{sec}"', page, sec)
        self.assertNotRegex(page, r"var secs=\['home'")                         # タブの一覧を手で書かない（足し忘れで開けなくなる）

    def test_validation(self):
        self.assertEqual(self.send(text="短い").get_json()["code"], "too_short")
        self.assertEqual(self.send(kind="x").get_json()["code"], "kind")
        self.assertEqual(self.send(text="あ" * 1001).get_json()["code"], "too_long")
        self.assertEqual(self.send(text="クソみたいな作りで本当に最悪でした。直してください。").get_json()["code"], "bad_words")
        self.assertTrue(self.send(text="不具合です。https://example.com の画面で止まります。").get_json()["ok"])   # URL は書ける
        self.assertEqual(self.c.post("/api/box", data="[1]", content_type="application/json").status_code, 400)
        self.assertEqual(len(G.feedback_list()), 1)

    def test_bots_are_dropped_quietly(self):
        self.assertEqual(self.send(website="http://spam.example").get_json(), {"ok": True})
        self.assertEqual(G.feedback_list(), [])

    def test_rate_limit(self):
        with patch.object(A, "_rate_ok", return_value=False):
            self.assertEqual(self.send().status_code, 429)

    def test_text_is_escaped_in_admin(self):
        self.send(text="<script>alert(1)</script> が動かないか確認してください。")
        self.login()
        page = self.c.get("/admin").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)", page)

    def test_admin_done_and_delete(self):
        self.send()
        fid = G.feedback_list()[0]["id"]
        self.assertEqual(self.admin("box_done", id=fid).status_code, 403)                      # ログイン前は不可
        self.login()
        self.admin("box_done", id=fid)
        self.assertEqual((G.feedback_list()[0]["done"], G.feedback_open_count()), (1, 0))
        self.admin("box_undo", id=fid)
        self.assertEqual(G.feedback_open_count(), 1)
        self.admin("box_delete", id=fid)
        self.assertEqual(G.feedback_list(), [])


class ServedPages(Base):
    PAGES = ("/", "/nyanko", "/tsum", "/puni", "/ticket", "/support", "/source", "/reviews", "/box", "/admin")

    def test_csp_and_nonce_on_every_script(self):
        nonces = set()
        for path in self.PAGES:
            r = self.c.get(path)
            csp = r.headers["Content-Security-Policy"]
            nonce = re.search(r"script-src 'self' 'nonce-([\w-]+)'", csp).group(1)
            nonces.add(nonce)
            html = r.get_data(as_text=True)
            tags = re.findall(r"<script[^>]*>", html)
            self.assertTrue(tags or path == "/admin", path)
            self.assertTrue(all(f'nonce="{nonce}"' in t for t in tags), path)
            self.assertNotRegex(html, r"\son[a-z]+=\"", path)                                   # HTML に直接書いた onclick などは無い
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline'; script", csp)
            self.assertNotIn("'unsafe-eval'", csp)
            self.assertEqual(r.headers["Cache-Control"], "no-store", path)
        self.assertEqual(len(nonces), len(self.PAGES))                                          # 毎回ちがう

    def test_admin_page_scripts_have_nonce_too(self):
        self.login()
        r = self.c.get("/admin")
        nonce = re.search(r"'nonce-([\w-]+)'", r.headers["Content-Security-Policy"]).group(1)
        html = r.get_data(as_text=True)
        tags = re.findall(r"<script[^>]*>", html)
        self.assertTrue(tags and all(f'nonce="{nonce}"' in t for t in tags))
        self.assertIn("data-confirm=", html)
        self.assertNotIn("guard.js", html)                                                      # 管理画面には、のぞき見よけを入れない

    def test_no_explanatory_comments_are_served(self):
        for path in self.PAGES[:-1]:
            html = self.c.get(path).get_data(as_text=True)
            self.assertNotRegex(html, r"/\*[^*\n]*[぀-ヿ一-鿿]", path)
            self.assertNotIn("<!--", html, path)
            self.assertIn("guard.js", html, path)
        for name in ("shop.js", "review.js", "qa.js", "qa.css", "guard.js"):
            r = self.c.get("/static/" + name)
            body = r.get_data(as_text=True)
            self.assertEqual(r.status_code, 200)
            self.assertNotRegex(body, r"/\*[^*\n]*[぀-ヿ一-鿿]", name)
            self.assertLess(len(body), os.path.getsize(os.path.join("static", name)))
        self.assertEqual(self.c.get("/static/nope.js").status_code, 404)
        self.assertEqual(self.c.get("/static/../app.py").status_code, 404)
        self.assertTrue(self.c.get("/static/icon-192.png").data.startswith(b"\x89PNG"))        # 画像はそのまま

    def test_strip_comments_keeps_code(self):
        src = "\n".join([
            "/* 説明 */", "// 説明", "var a = 'http://example.com/x'; // 説明です", "var b = 1; /* 説明 */ var c = 2;",
            "/* 複数行の", "   説明 */", "var u = '/* not a comment */';", "var d = a // b", "x = {{ n // 60 }}時間 // 説明",
            "var e = 'これは文字列'; // 'quoted' 説明", ".k{color:red} /* 色 */",
        ])
        out = A.strip_comments(src)
        self.assertEqual(out.split("\n"), [
            "var a = 'http://example.com/x';", "var b = 1;  var c = 2;", "var u = '/* not a comment */';", "var d = a // b",
            "x = {{ n // 60 }}時間 // 説明", "var e = 'これは文字列'; // 'quoted' 説明", ".k{color:red}",
        ])
        page = "<p>10 // 2 は 5 です</p><!-- 消す --><script>\n// 消す\nvar a=1;\n</script>"
        self.assertEqual(A.strip_comments(page, html=True), "<p>10 // 2 は 5 です</p><script>\nvar a=1;\n</script>")

    def test_apis_are_not_stored(self):
        for path in ("/api/state", "/api/orders/mine", "/api/cats", "/api/reviews"):
            self.assertEqual(self.c.get(path).headers.get("Cache-Control"), "no-store", path)


class SmallChanges(Base):
    def test_review_shows_time_in_japan(self):
        G.review_add("O1", "nyanko", 5, "とても良かったです。また使います。")
        when = A._public_reviews(G.review_list())[0]["date"]
        self.assertRegex(when, r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$")
        self.assertEqual(A._jst(0), "1970/01/01 09:00")                                         # サーバーの時計の地域によらず日本時間
        self.assertIn(when, self.c.get("/reviews").get_data(as_text=True))
        self.assertEqual(self.c.get("/api/reviews").get_json()["reviews"][0]["date"], when)

    def test_cancel_button_everywhere_but_not_for_admin(self):
        for path, bid in (("/nyanko", "cancel-queue"), ("/tsum", "cancelQueue"), ("/puni", "cancelQueue")):
            html = self.c.get(path).get_data(as_text=True)
            self.assertIn(f'id="{bid}"', html)
            self.assertIn("やっぱりやめる", html)
            self.assertRegex(html, r"hidden=IS_ADMIN\|\|(st|j\.status)!=='queued'", path)        # 順番待ちのあいだだけ・管理者には出さない

    def test_cancel_keeps_the_payment_for_a_free_rerun(self):
        with A._jobs_lock:
            A._jobs["JQ"] = {"status": "queued", "owner": None}
            A._pending_jobs.append(("JQ", (), None))
        try:
            with self.c.session_transaction():
                pass
            with patch.object(A, "_owner", return_value=None):
                r = self.c.post("/cancel/JQ")
            self.assertEqual(r.get_json(), {"ok": True})
            self.assertEqual(A._jobs["JQ"]["status"], "error")
            self.assertIn("追加料金なし", A._jobs["JQ"]["error"])
            with patch.object(A, "_owner", return_value=None):
                self.assertEqual(self.c.post("/cancel/JQ").status_code, 409)                   # もう待っていない
        finally:
            for e in list(A._pending_jobs):
                if e[0] == "JQ":
                    A._pending_jobs.remove(e)

    def test_nyanko_help_does_not_open_by_itself(self):
        html = self.c.get("/nyanko").get_data(as_text=True)
        self.assertNotRegex(html, r"(?m)^openHelp\(\);")
        self.assertIn('id="firstTip" hidden', html)                                             # はじめての人には、細い案内だけ
        self.assertIn('id="helpPanel"', html)                                                   # 自分で開く「使い方」は残す


class TwoStepLogin(Base):
    def code(self, offset=0):
        secret = admin_2fa._load()
        return admin_2fa.code_at(secret, int(time.time() // 30) + offset)

    def enable(self):
        self.login()
        self.admin("tfa_begin")
        page = self.c.get("/admin").get_data(as_text=True)
        self.assertIn("otpauth://totp/", page)
        secret = admin_2fa._pending["secret"]
        self.assertIn(" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), page)
        self.admin("tfa_confirm", code="000000")
        self.assertFalse(admin_2fa.enabled())                                                   # 間違った6桁では有効にならない
        self.admin("tfa_confirm", code=admin_2fa.code_at(secret, int(time.time() // 30)))
        self.assertTrue(admin_2fa.enabled())
        return secret

    def test_rfc6238_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"                                             # RFC 6238 の見本の鍵（"12345678901234567890"）
        self.assertEqual(admin_2fa.code_at(secret, 59 // 30), "287082")
        self.assertEqual(admin_2fa.code_at(secret, 1111111109 // 30), "081804")

    def test_off_by_default(self):
        self.assertFalse(admin_2fa.enabled())
        self.login()
        self.assertTrue(self.is_admin())

    def test_login_needs_the_code_once_enabled(self):
        secret = self.enable()
        raw = Path(self.tmp.name, "admin_2fa.json").read_text()
        self.assertNotIn(secret, raw)                                                           # 鍵は暗号化して置く
        self.assertNotIn(secret, self.c.get("/admin").get_data(as_text=True))                   # 有効にしたあとは、鍵を二度と見せない
        c = A.app.test_client()
        self.login(c)
        self.assertFalse(self.is_admin(c))                                                      # パスコードだけでは入れない
        self.assertIn("6桁の番号", c.get("/admin").get_data(as_text=True))
        self.assertEqual(self.admin("order_dead", c, order="x").status_code, 403)
        self.admin("unlock_2fa", c, code="123456")
        self.assertFalse(self.is_admin(c))
        good = self.code(1)
        self.admin("unlock_2fa", c, code=good)
        self.assertTrue(self.is_admin(c))
        c2 = A.app.test_client()                                                                # 同じ6桁は、もう使えない
        self.login(c2)
        self.admin("unlock_2fa", c2, code=good)
        self.assertFalse(self.is_admin(c2))

    def test_code_without_password_is_useless(self):
        self.enable()
        c = A.app.test_client()
        self.admin("unlock_2fa", c, code=self.code(1))
        self.assertFalse(self.is_admin(c))

    def test_code_step_expires(self):
        self.enable()
        c = A.app.test_client()
        self.login(c)
        with c.session_transaction() as s:
            s["admin_2fa_at"] = time.time() - A.ADMIN_2FA_STEP_SEC - 5
        self.admin("unlock_2fa", c, code=self.code(1))
        self.assertFalse(self.is_admin(c))

    def test_wrong_codes_are_rate_limited(self):
        self.enable()
        c = A.app.test_client()
        self.login(c)
        for _ in range(5):
            self.admin("unlock_2fa", c, code="000000")
        self.admin("unlock_2fa", c, code=self.code(1))
        self.assertFalse(self.is_admin(c))

    def test_disable_needs_a_code(self):
        self.enable()
        self.admin("tfa_disable", code="000000")
        self.assertTrue(admin_2fa.enabled())
        self.admin("tfa_disable", code=self.code(1))
        self.assertFalse(admin_2fa.enabled())

    def test_unreadable_key_never_lets_people_in(self):
        self.enable()
        with patch.dict(os.environ, {"PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode()}):   # 鍵が変わって読めない
            c = A.app.test_client()
            self.login(c)
            self.admin("unlock_2fa", c, code="123456")
            self.assertFalse(self.is_admin(c))

    def test_login_is_kept_not_logged_out_when_idle(self):
        # v1.28：無操作でのログアウトはやめた。長く触らなくても管理者のまま。
        self.login()
        self.assertTrue(self.is_admin())
        with self.c.session_transaction() as s:
            s["admin_seen"] = time.time() - 10 * 86400             # 10日さわっていない
        self.assertTrue(self.is_admin())


if __name__ == "__main__":
    unittest.main()
