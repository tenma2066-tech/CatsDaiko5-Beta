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
        pg.goto(self.url)
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        self.assertNotIn("OLD111aaa", s["hist"])

    def test_reload_same_tab_never_shows_old_code(self):
        """同じタブを再読み込みして戻ってきた。"""
        pg, _ = self._page(live=True)
        pg.goto(self.url)
        pg.evaluate("()=>{sessionStorage.setItem('job','J2'); sessionStorage.setItem('nyanko-seen-jobs',JSON.stringify(['J2']));}")
        pg.reload()
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        self.assertNotIn("OLD111aaa", s["hist"])

    def test_after_expiry_shows_newest_once(self):
        """30分以上たち、今回のジョブもサーバーから消えた：いちばん新しい注文を1回だけ出す。"""
        pg, _ = self._page(live=False)
        pg.goto(self.url)
        s = self._shown(pg)
        self.assertEqual(s["done"], ["NEW222bbb", "2222"])
        pg.reload()                                   # もう一度開いても、同じコードを何度も自動で出さない・前のコードも出さない
        s = self._shown(pg)
        self.assertIsNone(s["done"])
        self.assertEqual(s["hist"], ["NEW222bbb"])

    def test_job_seen_then_expired_does_not_pop_old_or_again(self):
        """今回の結果を画面で見たあと、時間がたってから開いた：前のコードも今回のコードも、勝手に出さない。"""
        pg, state = self._page(live=True)
        pg.goto(self.url)
        self.assertEqual(self._shown(pg)["done"], ["NEW222bbb", "2222"])
        state["live"] = False
        pg.reload()
        s = self._shown(pg)
        self.assertIsNone(s["done"])
        self.assertNotIn("OLD111aaa", s["hist"])


@unittest.skipIf(sync_playwright is None, "Playwright がない")
class ReviewGateSurvivesLeavingThePage(ResultCodesNotMixedUp):
    """「X でリプライする」を押すと、X のアプリ内ブラウザではページが移動し、iPhone では裏に回ったタブが
    捨てられることがある。戻ってきてページが読み込み直されても、押したことを覚えていてコードを出す
    （2026-11 の「リプライを押してもコードが見られない」報告の再発防止）。"""

    def _review_page(self):
        pg, _ = self._page(live=True)
        pg.context.route("https://x.com/**", lambda r, *_: r.fulfill(status=200, content_type="text/html", body="<p>x</p>"))
        pg.context.clock.install()
        pg.goto(self.url)
        pg.wait_for_timeout(5000)
        return pg

    def _locked(self, pg):
        return pg.evaluate("()=>({panel: !document.querySelector('#donePanel').hidden, "
                           "locked: document.querySelector('#donePanel').classList.contains('rvlock'), "
                           "review: !document.querySelector('#reviewPanel').hidden})")

    def test_before_pressing_reply_codes_are_locked(self):
        pg = self._review_page()
        self.assertEqual(self._locked(pg), {"panel": True, "locked": True, "review": True})

    def test_reply_then_page_reloaded_unlocks_after_wait(self):
        pg = self._review_page()
        with pg.context.expect_page() as popup:
            pg.click("#reviewLink")
        popup.value.close()
        pg.reload()                                   # 戻ってきたら読み込み直されていた
        pg.clock.fast_forward(25_000)
        pg.wait_for_timeout(5000)
        st = self._locked(pg)
        self.assertFalse(st["locked"], st)
        self.assertFalse(st["review"], st)

    def test_reply_then_back_in_a_new_tab_unlocks(self):
        pg = self._review_page()
        with pg.context.expect_page() as popup:
            pg.click("#reviewLink")
        popup.value.close()
        pg.clock.fast_forward(25_000)
        pg2 = pg.context.new_page()                   # X のアプリから新しいタブで戻ってきた
        pg2.route("**/api/orders/mine", lambda r, *_: r.fulfill(status=200, content_type="application/json",
                                                                 body=json.dumps(_orders(True, int(time.time())))))
        pg2.route("**/status/J2", lambda r, *_: r.fulfill(status=200, content_type="application/json", body=json.dumps(STATUS2)))
        pg2.goto(self.url)
        pg2.wait_for_timeout(5000)
        st = self._locked(pg2)
        self.assertFalse(st["locked"], st)

    def test_coming_back_too_early_still_waits(self):
        pg = self._review_page()
        with pg.context.expect_page() as popup:
            pg.click("#reviewLink")
        popup.value.close()
        pg.reload()
        pg.clock.fast_forward(5_000)                  # 5秒で戻ってきた：まだ出さない
        pg.wait_for_timeout(3000)
        self.assertTrue(self._locked(pg)["locked"])
        pg.clock.fast_forward(20_000)                 # 押してから20秒たった：出す（もう一度押さなくてよい）
        pg.wait_for_timeout(3000)
        self.assertFalse(self._locked(pg)["locked"])

    # 親クラスのテストはここでは繰り返さない
    test_new_tab_while_job_alive_shows_new_code = None
    test_reload_same_tab_never_shows_old_code = None
    test_after_expiry_shows_newest_once = None
    test_job_seen_then_expired_does_not_pop_old_or_again = None


if __name__ == "__main__":
    unittest.main()
