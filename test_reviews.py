"""実績（星5段階＋感想）のテスト。完了した注文1件につき1つだけ書けること、公開ページ、管理者の非表示。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as A
import paypay_gateway as G
import site_config

GOOD = "注文から5分で完了しました。案内が分かりやすくて安心でした。"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True),
            patch.object(G, "history_upsert"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
            patch.object(A, "_reconcile_orders"),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def done_order(self, c=None, mode=None, job_id="J1", **extra):
        """このブラウザの「完了した代行の注文」を1件作る。"""
        c = c or self.c
        j = c.post("/run", json={"items": ["XP"]}).get_json()
        o = A._orders[j["order"]]
        o.update(state="done", job_id=job_id, **extra)
        if mode:
            o["job"]["mode"] = mode
        return j["order"]

    def post(self, body, c=None):
        return (c or self.c).post("/api/reviews", json=body)


class Posting(Base):
    def test_review_is_saved_once_per_order(self):
        oid = self.done_order()
        r = self.post({"stars": 5, "text": GOOD, "order": oid})
        self.assertEqual((r.status_code, r.get_json()), (200, {"ok": True, "saved": True}))
        self.assertTrue(A._orders[oid]["reviewed"])
        rows = G.review_list()
        self.assertEqual([(x["stars"], x["body"], x["shop"]) for x in rows], [(5, GOOD, "nyanko")])
        self.assertNotIn("order_id", rows[0])                          # 注文番号は外へ出さない
        again = self.post({"stars": 1, "text": GOOD + "もう一度", "order": oid}).get_json()
        self.assertEqual(again, {"ok": True, "saved": False})          # 2回目は保存しない（でも画面は先へ進める）
        self.assertEqual(len(G.review_list()), 1)

    def test_found_by_job_or_latest_done_order(self):
        self.done_order(job_id="JA", created=time.time() - 100)
        b = self.done_order(job_id="JB", mode="tsum")
        self.assertTrue(self.post({"stars": 4, "text": GOOD, "job": "JB"}).get_json()["saved"])
        self.assertTrue(A._orders[b]["reviewed"])
        self.assertTrue(self.post({"stars": 3, "text": GOOD}).get_json()["saved"])   # 指定なし → 残っている完了注文
        self.assertEqual(sorted(x["shop"] for x in G.review_list()), ["nyanko", "tsum"])

    def test_only_completed_service_orders_of_this_browser(self):
        other = A.app.test_client()
        theirs = self.done_order(c=other)
        self.assertFalse(self.post({"stars": 5, "text": GOOD, "order": theirs}).get_json()["saved"])   # 他人の注文
        mine = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]                        # 未払い
        self.assertFalse(self.post({"stars": 5, "text": GOOD, "order": mine}).get_json()["saved"])
        t = self.c.post("/api/ticket/order", json={"plan": "once"}).get_json()["order"]              # チケットは対象外
        A._orders[t]["state"] = "done"
        self.assertFalse(self.post({"stars": 5, "text": GOOD, "order": t}).get_json()["saved"])
        self.assertEqual(G.review_list(), [])

    def test_validation(self):
        oid = self.done_order()
        for body, code in (({"stars": 0, "text": GOOD}, "bad_stars"), ({"stars": 6, "text": GOOD}, "bad_stars"),
                           ({"stars": "5", "text": GOOD}, "bad_stars"), ({"stars": True, "text": GOOD}, "bad_stars"),
                           ({"stars": 5, "text": "短い感想です"}, "too_short"),
                           ({"stars": 5, "text": "あ" * 19 + "   \n\n  "}, "too_short"),      # 空白で水増ししても数えない
                           ({"stars": 5, "text": "あ" * 30}, "too_plain"),
                           ({"stars": 5, "text": "あいうえおかきくけこ" * 41}, "too_long")):
            r = self.post(dict(body, order=oid))
            self.assertEqual((r.status_code, r.get_json()["code"]), (400, code), body)
        self.assertFalse(A._orders[oid].get("reviewed"))
        self.assertEqual(G.review_list(), [])
        r = self.post({"stars": 5, "text": "  " + GOOD + "\x00\x07  ", "order": oid})          # 制御文字は落として保存
        self.assertTrue(r.get_json()["saved"])
        self.assertEqual(G.review_list()[0]["body"], GOOD)

    def test_save_failure_can_be_retried(self):
        oid = self.done_order()
        with patch.object(G, "review_add", side_effect=RuntimeError("db")), self.assertLogs("orders", level="ERROR"):
            r = self.post({"stars": 5, "text": GOOD, "order": oid})
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "save_failed"))
        self.assertFalse(A._orders[oid].get("reviewed"))                # 印を戻すので、もう一度送れる
        self.assertTrue(self.post({"stars": 5, "text": GOOD, "order": oid}).get_json()["saved"])

    def test_orders_api_tells_if_reviewed(self):
        oid = self.done_order(result={"transfer": "abc", "confirm": "1234"})
        row = lambda: next(o for o in self.c.get("/api/orders/mine").get_json()["orders"] if o["order"] == oid)  # noqa: E731
        self.assertFalse(row()["reviewed"])
        self.post({"stars": 5, "text": GOOD, "order": oid})
        self.assertTrue(row()["reviewed"])


class PublicPage(Base):
    def add(self, n, stars=5, shop="nyanko"):
        for i in range(n):
            G.review_add(f"O{shop}{stars}{i}{time.time()}", shop, stars, f"{GOOD}（{i}）")

    def test_page_shows_average_and_reviews(self):
        self.add(3, 5)
        self.add(1, 2, "puni")
        with patch.object(site_config, "REVIEW_POST_ID", "1234567890"), patch.object(site_config, "SHOP_X_ID", "test_shop"):
            page = self.c.get("/reviews").get_data(as_text=True)
            past = site_config.past_reviews_url()
        self.assertEqual(past, "https://x.com/test_shop/status/1234567890")
        self.assertIn("4件の評価", page)
        self.assertIn("4.2", page)                                      # (5+5+5+2)/4 = 4.25 → 4.2
        self.assertIn(GOOD, page)
        self.assertIn("ぷにぷに代行", page)
        self.assertIn("今までの実績を見る", page)                      # これまでの X のポストへのリンク
        self.assertIn(past, page)
        self.assertEqual(G.review_stats()["dist"], {1: 0, 2: 1, 3: 0, 4: 0, 5: 3})

    def test_empty_page_and_home_banner(self):
        self.assertIn("まだ実績はありません", self.c.get("/reviews").get_data(as_text=True))
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn('class="rvbar" href="/reviews"', home)
        self.assertLess(home.index('class="rvbar"'), home.index('class="grid"'))   # メニューより上に出る
        self.add(2, 4)
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("（2件）", home)
        self.assertIn("4.0", home)

    def test_text_is_escaped(self):
        G.review_add("OX", "nyanko", 5, "<script>alert(1)</script> とても良かったです。また使います。")
        page = self.c.get("/reviews").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_paging(self):
        self.add(A.REVIEW_PAGE + 5)
        page = self.c.get("/reviews").get_data(as_text=True)
        self.assertIn('id="more" type="button" >', page)                # 続きがあるときだけボタンを出す
        first = G.review_list(A.REVIEW_PAGE)
        j = self.c.get(f"/api/reviews?before={first[-1]['id']}").get_json()
        self.assertEqual((len(j["reviews"]), j["more"]), (5, False))
        self.assertEqual(set(j["reviews"][0]), {"id", "stars", "text", "shop", "date"})
        self.assertEqual(self.c.get("/api/reviews?before=abc").status_code, 200)

    def test_can_be_switched_off(self):
        with patch.object(site_config, "REVIEWS_ENABLED", False):
            self.assertEqual(self.c.get("/reviews").status_code, 302)
            self.assertEqual(self.c.post("/api/reviews", json={"stars": 5, "text": GOOD}).status_code, 404)
            self.assertNotIn("rvbar\" href", self.c.get("/").get_data(as_text=True))
            self.assertNotIn("/reviews", self.c.get("/sitemap.xml").get_data(as_text=True))
            self.assertIn("const REVIEWS_ON=false&&", self.c.get("/nyanko").get_data(as_text=True))
        self.assertIn("/reviews", self.c.get("/sitemap.xml").get_data(as_text=True))


class Moderation(Base):
    def test_admin_can_hide_and_show(self):
        G.review_add("O1", "nyanko", 5, GOOD)
        G.review_add("O2", "tsum", 1, "これは表示したくない内容の感想です。テスト用の文章。")
        bad = next(r for r in G.review_list() if r["stars"] == 1)["id"]
        with patch.object(A, "ADMIN_PASSWORD", "pw"), patch.object(G, "status", return_value={"connected": False, "ready": False, "expires_at": 0}), \
             patch.object(G, "history_list", return_value=[]):
            self.c.get("/admin")
            with self.c.session_transaction() as s:
                s["admin"], s["admin_at"], csrf = True, time.time(), s["admin_csrf"]
            self.assertIn("表示したくない", self.c.get("/admin").get_data(as_text=True))
            self.c.post("/admin", data={"csrf": csrf, "action": "review_hide", "id": str(bad)})
            self.assertNotIn("表示したくない", self.c.get("/reviews").get_data(as_text=True))
            self.assertEqual(G.review_stats()["count"], 1)              # 平均・件数からも外れる
            self.assertIn("表示したくない", self.c.get("/admin").get_data(as_text=True))   # 管理画面には残る
            self.c.post("/admin", data={"csrf": csrf, "action": "review_show", "id": str(bad)})
            self.assertEqual(G.review_stats()["count"], 2)
        # ログインしていない人は変えられない
        anon = A.app.test_client()
        anon.get("/admin")
        with anon.session_transaction() as s:
            token = s["admin_csrf"]
        self.assertEqual(anon.post("/admin", data={"csrf": token, "action": "review_hide", "id": str(bad)}).status_code, 403)
        self.assertEqual(G.review_stats()["count"], 2)


if __name__ == "__main__":
    unittest.main()
