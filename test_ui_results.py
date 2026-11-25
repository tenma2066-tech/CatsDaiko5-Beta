"""完了画面に出る引継ぎコードが、取り違えられないかのブラウザテスト（2026-11 の不具合の再発防止）。

前の注文（O1）と今回の注文（O2）がある状態で、タブを開き直す・再読み込み・時間がたつ などの場面で、
画面に出るコードと「この端末に残した結果」に、前の注文のコードが出てこないことを確かめる。
サーバーへの通信（/api/orders/mine・/status/<job>）はブラウザ側で差し替える。
Playwright と Chromium がない環境では飛ばす。
"""
import json
import os
import shutil
import threading
import time
import unittest
from unittest.mock import patch

import site_config

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

CHROME = os.environ.get("XBOT_CHROMIUM") or next(
    (p for p in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", shutil.which("chromium"), shutil.which("chromium-browser")) if p and os.path.exists(p)), None)

R1 = {"transfer": "OLD111aaa", "confirm": "1111"}
R2 = {"transfer": "NEW222bbb", "confirm": "2222"}


def _orders(job2_live, now):
    o2 = {"order": "O2", "amount": 100, "state": "done", "created": now, "retry": False, "result": R2, "clone": False,
          "coupon_issued": None, "breakdown": [], "skipped": [], "job": "J2" if job2_live else None}
    o1 = {"order": "O1", "amount": 100, "state": "done", "created": now - 3600, "retry": False, "result": R1, "clone": False,
          "coupon_issued": None, "breakdown": [], "skipped": [], "job": None}
    return {"orders": [o2, o1], "maint": {"on": False}}


STATUS2 = {"status": "done", "queue_position": None, "slot": None, "active_jobs": 0, "warmup_slots": 0, "occupied_slots": 0,
           "available_slots": 4, "max_concurrent_jobs": 4, "queued_jobs": 0, "step": 4, "total": 4, "mode": "x", "count": 1,
           "elapsed": 30, "log": [], "warn": [], "report": [], "dry": False, "clone": False, "outcome_unknown": False,
           "result": R2, "error": None, "integrity": None, "integrity_only": False, "audit": None, "repair": None, "post_audit": None}


@unittest.skipIf(sync_playwright is None, "Playwright がない")
class ResultCodesNotMixedUp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as A
        from werkzeug.serving import make_server
        cls.srv = make_server("127.0.0.1", 0, A.app, threaded=True)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:   # CI では playwright install で入れた Chromium、この作業環境では決まった場所の Chromium
            cls.browser = cls.pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            cls.pw.stop()
            cls.srv.shutdown()
            raise unittest.SkipTest(f"Chromium を起動できない: {type(exc).__name__}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def _page(self, live):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844})
        state = {"live": live}
        now = int(time.time())
        pg = ctx.new_page()
        pg.route("**/api/orders/mine", lambda r, *_: r.fulfill(status=200, content_type="application/json",
                                                                body=json.dumps(_orders(state["live"], now))))
        pg.route("**/status/J2", lambda r, *_: r.fulfill(status=200, content_type="application/json", body=json.dumps(STATUS2))
                 if state["live"] else r.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "見つかりません"})))
        self.addCleanup(ctx.close)
        return pg, state

    def _shown(self, pg):
        pg.wait_for_timeout(5000)   # time.sleep だとその間ブラウザへの応答（route）が止まる
        return pg.evaluate("""()=>({
            done: document.querySelector('#donePanel').hidden ? null : [...document.querySelectorAll('#doneCodes strong')].map(e=>e.textContent),
            hist: JSON.parse(localStorage.getItem('nyanko-results')||'[]').map(h=>h.result.transfer)})""")

    def test_new_tab_while_job_alive_shows_new_code(self):
        """リプライで X に移り、新しいタブで戻ってきた（今回のジョブはまだサーバーにある）。"""
        pg, _ = self._page(live=True)
        pg.goto(self.url + "nyanko")
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        self.assertNotIn("OLD111aaa", s["hist"])

    def test_reload_same_tab_never_shows_old_code(self):
        """同じタブを再読み込みして戻ってきた。"""
        pg, _ = self._page(live=True)
        pg.goto(self.url + "nyanko")
        pg.evaluate("()=>{sessionStorage.setItem('job','J2'); sessionStorage.setItem('nyanko-seen-jobs',JSON.stringify(['J2']));}")
        pg.reload()
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        self.assertNotIn("OLD111aaa", s["hist"])

    def test_after_expiry_shows_newest_once(self):
        """30分以上たち、今回のジョブもサーバーから消えた：いちばん新しい注文を1回だけ出す。"""
        pg, _ = self._page(live=False)
        pg.goto(self.url + "nyanko")
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        pg.reload()                                   # もう一度開いても、同じコードを何度も自動で出さない・前のコードも出さない
        s = self._shown(pg)
        self.assertIsNone(s["done"])
        self.assertEqual(s["hist"], ["NEW222bbb"])

    def test_job_seen_then_expired_does_not_pop_old_or_again(self):
        """今回の結果を画面で見たあと、時間がたってから開いた：前のコードも今回のコードも、勝手に出さない。"""
        pg, state = self._page(live=True)
        pg.goto(self.url + "nyanko")
        self.assertEqual(self._shown(pg)["done"], ["NEW222bbb", "2222"])
        state["live"] = False
        pg.reload()
        s = self._shown(pg)
        self.assertIsNone(s["done"])
        self.assertNotIn("OLD111aaa", s["hist"])


@unittest.skipIf(sync_playwright is None, "Playwright がない")
class ReviewGate(ResultCodesNotMixedUp):
    """完了したら、アプリの中で評価（星）と感想（20文字以上）を送ってもらってから結果を渡す。
    渡すかどうかはサーバーが決める：評価がまだの注文は、/status も注文一覧も結果を返さない（result_locked / locked）。
    画面は入力欄を出し、送信できたら結果を取り直す。ここではサーバーの返事を真似て、画面の動きを確かめる。"""

    GOOD = "注文から5分で完了しました。案内が分かりやすくて安心でした。"

    def setUp(self):
        super().setUp()
        p = patch.object(site_config, "REVIEWS_ENABLED", True)   # お店の設定に左右されないよう、このテストでは必ず有効
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.reviewed = False          # サーバーが評価を受け取ったか（受け取ったら結果を返す）

    def _review_page(self, status=200, body=None, accept=True):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        now = int(time.time())

        def orders(route, *_):
            o = dict(_orders(True, now)["orders"][0])
            if not self.reviewed:
                o.update(result=None, locked=True)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"orders": [o], "maint": {"on": False}}))

        def job(route, *_):
            st = dict(STATUS2)
            if not self.reviewed:
                st.update(result=None, result_locked=True)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(st))

        def review(route, *_):
            self.sent.append(json.loads(route.request.post_data or "{}"))
            if status == 200 and accept:
                self.reviewed = True
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(body if body is not None else {"ok": True, "saved": True}))
        pg.route("**/api/orders/mine", orders)
        pg.route("**/status/J2", job)
        pg.route("**/api/reviews", review)
        pg.goto(self.url + "nyanko")
        pg.wait_for_timeout(5000)
        return pg

    def _state(self, pg):
        return pg.evaluate("()=>({review: !document.querySelector('#reviewPanel').hidden, "
                           "done: !document.querySelector('#donePanel').hidden, "
                           "codes: [...document.querySelectorAll('#doneCodes strong')].map(e=>e.textContent), "
                           "page: document.body.innerText.includes('NEW222bbb')})")

    def _fill(self, pg, stars=5, text=None):
        pg.click(f"#reviewForm .rvf-stars button:nth-child({stars})")
        pg.fill("#reviewForm textarea", self.GOOD if text is None else text)

    def test_result_is_not_on_the_page_until_a_review_is_sent(self):
        pg = self._review_page()
        st = self._state(pg)
        self.assertEqual((st["review"], st["done"], st["page"]), (True, False, False), st)   # 入力欄だけ。コードは画面のどこにも無い
        self.assertNotIn("NEW222bbb", pg.content())
        self.assertTrue(pg.is_disabled("#reviewForm .rvf-send"))            # 星も感想もまだ
        pg.click("#reviewForm .rvf-stars button:nth-child(4)")
        self.assertTrue(pg.is_disabled("#reviewForm .rvf-send"))            # 星だけでは送れない
        pg.fill("#reviewForm textarea", "短い感想")
        self.assertTrue(pg.is_disabled("#reviewForm .rvf-send"))            # 20文字に足りない
        self.assertIn("あと 16 文字", pg.text_content("#reviewForm .rvf-count"))
        pg.fill("#reviewForm textarea", self.GOOD)
        self.assertFalse(pg.is_disabled("#reviewForm .rvf-send"))
        pg.keyboard.press("Escape")                                         # 閉じる手段は無い
        self.assertTrue(self._state(pg)["review"])

    def test_sending_brings_the_result_and_reload_does_not_ask_again(self):
        pg = self._review_page()
        self._fill(pg, stars=5)
        pg.click("#reviewForm .rvf-send")
        pg.wait_for_timeout(2500)
        st = self._state(pg)
        self.assertEqual((st["review"], st["done"], st["codes"]), (False, True, ["NEW222bbb", "2222"]), st)
        self.assertEqual((self.sent[0]["stars"], self.sent[0]["text"], self.sent[0]["job"]), (5, self.GOOD, "J2"))
        pg.reload()                                                         # 読み込み直しても、もう一度は聞かない
        pg.wait_for_timeout(5000)
        self.assertFalse(self._state(pg)["review"])
        self.assertEqual(len(self.sent), 1)

    def test_rejected_review_keeps_the_result_away(self):
        pg = self._review_page(status=400, body={"error": "不適切な言葉が含まれています。言葉を変えて、もう一度お書きください。", "code": "bad_words"})
        self._fill(pg)
        pg.click("#reviewForm .rvf-send")
        pg.wait_for_timeout(1000)
        self.assertIn("不適切な言葉", pg.text_content("#reviewForm .rvf-err"))
        st = self._state(pg)
        self.assertEqual((st["review"], st["page"]), (True, False), st)

    def test_server_trouble_never_keeps_the_customer_from_the_codes(self):
        # 保存の不調：1回目は 503。2回目でサーバーが免除（waived）して結果を渡す
        pg = self._review_page(status=503, body={"error": "保存できませんでした。もう一度お試しください。", "code": "save_failed"})
        self._fill(pg)
        pg.click("#reviewForm .rvf-send")
        pg.wait_for_timeout(1000)
        self.assertTrue(self._state(pg)["review"])                          # 1回目：やり直しをお願いする
        pg.unroute("**/api/reviews")

        def waive(route, *_):
            self.reviewed = True
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "saved": False, "waived": True}))
        pg.route("**/api/reviews", waive)
        pg.click("#reviewForm .rvf-send")
        pg.wait_for_timeout(2500)
        st = self._state(pg)
        self.assertEqual((st["review"], st["codes"]), (False, ["NEW222bbb", "2222"]), st)

    # 親クラスのテストはここでは繰り返さない
    test_new_tab_while_job_alive_shows_new_code = None
    test_reload_same_tab_never_shows_old_code = None
    test_after_expiry_shows_newest_once = None
    test_job_seen_then_expired_does_not_pop_old_or_again = None


@unittest.skipIf(sync_playwright is None, "Playwright がない")
class BackButtonBetweenGames(ResultCodesNotMixedUp):
    """トップ（大メニュー）→ にゃんこ／ツムツム → 戻る で、素直に大メニューへ戻れること（2026-11 の報告の再発防止）。"""

    def _where(self, pg):
        pg.wait_for_timeout(800)
        path = pg.evaluate("location.pathname")
        return path

    def test_back_returns_to_the_big_menu(self):
        pg, _ = self._page(live=False)
        pg.goto(self.url)
        self.assertEqual(self._where(pg), "/")
        pg.click("a.gamecard[href='/nyanko']")
        self.assertEqual(self._where(pg), "/nyanko")
        pg.go_back()
        self.assertEqual(self._where(pg), "/")
        self.assertTrue(pg.is_visible("a.gamecard[href='/tsum']"))
        pg.click("a.gamecard[href='/tsum']")
        self.assertEqual(self._where(pg), "/tsum")
        pg.go_back()
        self.assertEqual(self._where(pg), "/")
        pg.go_forward()
        self.assertEqual(self._where(pg), "/tsum")

    def test_reselect_links_go_to_the_big_menu(self):
        pg, _ = self._page(live=False)
        # 注文が無い人として開く（完了した注文があると、コードのお知らせが画面を覆うのが正しい動き）
        pg.route("**/api/orders/mine", lambda r, *_: r.fulfill(status=200, content_type="application/json",
                                                                body=json.dumps({"orders": [], "maint": {"on": False}})))
        pg.goto(self.url + "tsum")
        pg.click("text=← ゲームを選び直す")
        self.assertEqual(self._where(pg), "/")
        pg.goto(self.url + "nyanko")
        pg.wait_for_timeout(800)
        if pg.is_visible("#helpClose"):
            pg.click("#helpClose")
        pg.click("text=← ゲームを選び直す")
        self.assertEqual(self._where(pg), "/")
        pg.go_back()
        self.assertEqual(self._where(pg), "/nyanko")

    def test_old_entries_land_on_nyanko_without_a_back_loop(self):
        pg, _ = self._page(live=False)
        pg.goto(self.url + "?game=nyanko")
        self.assertEqual(self._where(pg), "/nyanko")
        pg.goto(self.url + "#nyanko")
        self.assertEqual(self._where(pg), "/nyanko")

    def test_home_points_to_unfinished_orders(self):
        """以前は / がにゃんこの画面だった。リプライのあと / を開き直した人が、コードの画面へ行けること。"""
        pg, _ = self._page(live=False)
        pg.goto(self.url)
        pg.wait_for_timeout(1500)
        self.assertTrue(pg.is_visible("#mineNyanko"))
        self.assertFalse(pg.is_visible("#mineTsum"))

    test_new_tab_while_job_alive_shows_new_code = None
    test_reload_same_tab_never_shows_old_code = None
    test_after_expiry_shows_newest_once = None
    test_job_seen_then_expired_does_not_pop_old_or_again = None


if __name__ == "__main__":
    unittest.main()
