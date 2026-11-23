"""ツムツム代行のテスト（ゲーム・LINE・PayPay への通信はすべてモック）。

ここで確かめること：
- メニューの検査（数量の範囲・同時に選べない組み合わせ・実行順）
- 料金が画面とサーバーで一致すること（画面はサーバーの値を出しているだけ）
- 1回の注文の実行（メール／引き継ぎ番号／新規ゲスト。引き継ぎ情報の発行まで）
- 注文の保存・読み込み（アカウント情報の形が崩れないこと）
- 画像認証（CAPTCHA）の受け渡し
"""
import types
import unittest
from unittest.mock import patch

import requests

import app as A
import paypay_gateway as G
import pricing
import tsum_service as T


def fake_core(**overrides):
    """tsum_engine の代わり。呼ばれた内容を記録して、決めた結果を返す。"""
    calls = []

    def forge_sync(**kw):
        calls.append(("forge_sync", kw))
        return {"retcode": 0}, "コイン: 0 → 100 ( +100 )\n完了しました。\nまたのご利用をお待ちしております。"

    def headless_session_direct(login_id, password, **kw):
        calls.append(("login", login_id))
        return {"userid": "12345678", "hash": "h" * 8, "checkval": "cv", "userToken": "ut"}

    def guest_takeover(migration_id, password, proxies=None):
        calls.append(("takeover", migration_id))
        return {"userid": "87654321", "hash": "h", "checkval": "cv", "userToken": "ut", "uuid": "u"}, None

    def guest_create(proxies=None, on_step=None):
        calls.append(("create", None))
        return {"userid": "11112222", "hash": "h", "checkval": "cv", "userToken": "ut", "uuid": "u"}, None

    def guest_issue_transfer(sess, proxies=None, migration_id=None):
        calls.append(("issue", sess.get("userid")))
        return "ab12cd34", "pw-9999"

    core = types.SimpleNamespace(
        calls=calls,
        requests=requests,
        MAX_PRESETS={"score_max": ("score", 2_147_483_647), "level_max": ("exp", 100_000_000),
                     "coin_max": ("coin", 200_000_000)},
        DEBUG_HOOK=None,
        forge_sync=forge_sync,
        Forge=lambda **kw: types.SimpleNamespace(get_public_key=lambda: "pub"),
        describe_login_failure=lambda raw: "ログインできませんでした",
        tsum_login=types.SimpleNamespace(headless_session_direct=headless_session_direct),
        tsum_guest=types.SimpleNamespace(guest_takeover=guest_takeover, guest_create=guest_create,
                                         guest_issue_transfer=guest_issue_transfer,
                                         mark_pending_done=lambda *a, **k: None),
        _unique_game_proxy=lambda: None,
        _game_api_proxy=lambda proxy: None,
        _proxies_dict=lambda proxy: None,
        _save_guest_transfer=lambda *a: None,
        _save_orphan_guest=lambda *a: None,
    )
    for key, value in overrides.items():
        setattr(core, key, value)
    return core


class Menus(unittest.TestCase):
    def test_sort_is_execution_order(self):
        # コイン → ツムレベル → ガチャ → スコア → メダル → レベル の順（ガチャはコインを使う）
        picked = ["tsum_level_max", "tsum_box", "tsum_coin", "tsum_medal", "tsum_tsum_lv"]
        self.assertEqual(T.sort_menus(picked),
                         ["tsum_coin", "tsum_tsum_lv", "tsum_box", "tsum_medal", "tsum_level_max"])

    def test_unknown_menu_is_dropped(self):
        self.assertEqual(T.sort_menus(["tsum_coin", "../etc/passwd", "nyanko"]), ["tsum_coin"])

    def test_no_menu(self):
        self.assertIn("メニューを選んで", T.validate_order([], {})[2])

    def test_value_required(self):
        self.assertIn("数量を入力", T.validate_order(["tsum_coin"], {})[2])
        self.assertIn("整数", T.validate_order(["tsum_coin"], {"tsum_coin": "１0０"})[2])
        self.assertIn("整数", T.validate_order(["tsum_coin"], {"tsum_coin": "-5"})[2])

    def test_value_limits(self):
        self.assertIn("範囲", T.validate_order(["tsum_coin"], {"tsum_coin": "200000001"})[2])
        self.assertIn("範囲", T.validate_order(["tsum_medal"], {"tsum_medal": "450001"})[2])
        self.assertIn("範囲", T.validate_order(["tsum_medal"], {"tsum_medal": "0"})[2])
        menus, qty, err = T.validate_order(["tsum_coin"], {"tsum_coin": "200000000"})
        self.assertEqual((menus, qty, err), (["tsum_coin"], {"tsum_coin": 200_000_000}, ""))

    def test_preset_menu_needs_no_value(self):
        self.assertEqual(T.validate_order(["tsum_coin_max"], {}), (["tsum_coin_max"], {}, ""))

    def test_guest_create_is_solo(self):
        self.assertIn("同時に選べません", T.validate_order(["tsum_guest_create", "tsum_coin"], {"tsum_coin": "1"})[2])
        self.assertEqual(T.validate_order(["tsum_guest_create"], {})[0], ["tsum_guest_create"])

    def test_account_check(self):
        self.assertIn("入力方法", T.check_account("x", "a", "b"))
        self.assertEqual(T.check_account("new", "", ""), "")
        self.assertIn("メールアドレスとパスワード", T.check_account("line", "", ""))
        self.assertIn("引き継ぎ番号とパスワード", T.check_account("guest", "", ""))
        self.assertIn("電話番号ではログインできません", T.check_account("line", "09012345678", "pw"))
        self.assertEqual(T.check_account("line", "a@example.com", "pw"), "")
        self.assertEqual(T.check_account("guest", "ab12cd34", "pw"), "")


class Prices(unittest.TestCase):
    def test_amount_is_sum_of_menu_prices(self):
        amount, rows = pricing.compute_tsum_amount(["tsum_box", "tsum_coin"], {"tsum_coin": 100})
        self.assertEqual(amount, pricing.PRICES["tsum_coin"] + pricing.PRICES["tsum_box"])
        self.assertEqual([r[1] for r in rows], [1, 1])

    def test_quantity_does_not_change_amount(self):
        a1, _ = pricing.compute_tsum_amount(["tsum_coin"], {"tsum_coin": 1})
        a2, _ = pricing.compute_tsum_amount(["tsum_coin"], {"tsum_coin": 200_000_000})
        self.assertEqual(a1, a2)

    def test_every_menu_has_a_price(self):
        for key, menu in T.MENUS.items():
            self.assertIn(menu["price"], pricing.PRICES, key)
            self.assertIn(menu["price"], pricing.PRICE_LABELS, key)

    def test_page_shows_the_same_amount_as_the_server(self):
        """画面の金額はサーバーが埋め込んだ値。1メニューずつ、画面とサーバーが一致することを確かめる。"""
        client = A.app.test_client()
        page = client.get("/tsum").get_data(as_text=True)
        for key, menu in T.MENUS.items():
            amount, _ = pricing.compute_tsum_amount([key], {})
            self.assertEqual(amount, pricing.PRICES[menu["price"]], key)
            self.assertIn(f'<b>{menu["label"]} <em>{amount}円</em></b>', page)


class RunOrder(unittest.TestCase):
    def setUp(self):
        self.core = fake_core()
        self.p = patch.object(T, "_core", self.core)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_line_mode_runs_without_issuing_a_transfer(self):
        out = T.run_order("line", "a@example.com", "pw", ["tsum_coin"], {"tsum_coin": 100})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["done"], 1)
        self.assertFalse(out["moved"])
        self.assertIsNone(out["transfer"])
        self.assertIn("■ コイン指定：完了", out["lines"])
        self.assertEqual([c for c in self.core.calls if c[0] == "issue"], [])

    def test_menus_run_in_execution_order_with_one_login(self):
        out = T.run_order("line", "a@example.com", "pw",
                          ["tsum_box", "tsum_coin"], {"tsum_coin": 50})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["done"], 2)
        logins = [c for c in self.core.calls if c[0] == "login"]
        self.assertEqual(len(logins), 1)
        sent = [c[1] for c in self.core.calls if c[0] == "forge_sync"]
        self.assertEqual([("coin" in k) for k in sent], [True, False])
        self.assertEqual(sent[1]["box"], T.DEFAULT_SELECT_BOX_ID)

    def test_preset_values(self):
        T.run_order("line", "a@example.com", "pw", ["tsum_coin_max", "tsum_score_max", "tsum_level_max"], {})
        sent = [c[1] for c in self.core.calls if c[0] == "forge_sync"]
        self.assertEqual(sent[0]["coin"], 200_000_000)
        self.assertEqual(sent[1]["score"], 2_147_483_647)
        self.assertEqual(sent[2]["exp"], 100_000_000)

    def test_tsum_level_uses_the_set_mytsum(self):
        T.run_order("line", "a@example.com", "pw", ["tsum_tsum_lv"], {})
        sent = [c[1] for c in self.core.calls if c[0] == "forge_sync"][0]
        self.assertEqual(sent["tsum_lv"], T.MYTSUM_AUTO)

    def test_guest_mode_issues_a_new_transfer(self):
        out = T.run_order("guest", "old12345", "pw", ["tsum_coin"], {"tsum_coin": 100})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["moved"])
        self.assertEqual((out["transfer"], out["confirm"]), ("ab12cd34", "pw-9999"))

    def test_guest_mode_issues_a_transfer_even_when_the_run_fails(self):
        self.core.forge_sync = lambda **kw: (None, "ハートが足りないため中断しました")
        out = T.run_order("guest", "old12345", "pw", ["tsum_coin"], {"tsum_coin": 100})
        self.assertFalse(out["ok"])
        self.assertIn("ハート", out["error"])
        self.assertEqual(out["transfer"], "ab12cd34")   # アカウントはこちらに移っているので必ず返す

    def test_banned_guest_account_stops_and_still_returns_a_transfer(self):
        self.core.tsum_guest.guest_takeover = lambda mid, pw, proxies=None: (
            {"userid": "9", "banned": True, "ban_msg": "不正利用", "uuid": "u"}, None)
        out = T.run_order("guest", "old12345", "pw", ["tsum_coin"], {"tsum_coin": 100})
        self.assertFalse(out["ok"])
        self.assertIn("利用停止", out["error"])
        self.assertEqual(out["transfer"], "ab12cd34")

    def test_takeover_failure_returns_the_reason(self):
        self.core.tsum_guest.guest_takeover = lambda mid, pw, proxies=None: (None, "番号かパスワードが違います")
        out = T.run_order("guest", "old12345", "pw", ["tsum_coin"], {"tsum_coin": 100})
        self.assertFalse(out["ok"])
        self.assertIn("違います", out["error"])
        self.assertIsNone(out["transfer"])
        self.assertFalse(out["moved"])

    def test_guest_create(self):
        out = T.run_order("new", "", "", ["tsum_guest_create"], {})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["transfer"], "ab12cd34")
        self.assertEqual([c for c in self.core.calls if c[0] == "forge_sync"], [])

    def test_login_failure_is_reported_without_credentials(self):
        self.core.tsum_login.headless_session_direct = lambda *a, **k: None
        out = T.run_order("line", "a@example.com", "secret-pw", ["tsum_coin"], {"tsum_coin": 1})
        self.assertFalse(out["ok"])
        self.assertNotIn("secret-pw", out["error"])
        self.assertNotIn("a@example.com", out["error"])

    def test_network_error_during_login_is_retried_then_reported(self):
        tries = []

        def boom(*a, **k):
            tries.append(1)
            raise requests.exceptions.ConnectionError("down")

        self.core.tsum_login.headless_session_direct = boom
        out = T.run_order("line", "a@example.com", "pw", ["tsum_coin"], {"tsum_coin": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(len(tries), 3)
        self.assertIn("接続が不安定", out["error"])

    def test_captcha_answer_is_passed_to_the_engine(self):
        seen = {}

        def login(login_id, password, **kw):
            seen["text"] = kw["captcha_solver"](b"PNGDATA")
            return {"userid": "1", "hash": "h", "checkval": "c", "userToken": "t"}

        self.core.tsum_login.headless_session_direct = login
        out = T.run_order("line", "a@example.com", "pw", ["tsum_coin"], {"tsum_coin": 1},
                          captcha=lambda img: "AB12")
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen["text"], "AB12")

    def test_progress_and_log_never_contain_the_password(self):
        seen = []
        T.run_order("line", "a@example.com", "secret-pw", ["tsum_coin"], {"tsum_coin": 1},
                    progress=lambda step, text: seen.append(text), log=seen.append)
        self.assertTrue(seen)
        self.assertFalse([s for s in seen if "secret-pw" in s or "a@example.com" in s])


class Order(unittest.TestCase):
    """注文（/run）と保存の形。ゲームへの通信はしない。"""

    def setUp(self):
        self.p = patch.object(A, "PAYMENTS_ENABLED", True)
        self.p.start()
        self.o = patch.dict(A._orders, {}, clear=True)
        self.o.start()
        self.j = patch.dict(A._jobs, {}, clear=True)
        self.j.start()
        self.h = patch.object(G, "history_upsert")
        self.h.start()
        self.s = patch.object(G, "save_orders")
        self.s.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in (self.s, self.h, self.j, self.o, self.p):
            p.stop()

    def body(self, **over):
        out = {"tsum": {"mode": "line", "login": "a@example.com", "password": "pw",
                        "menus": ["tsum_coin"], "qty": {"tsum_coin": "100"}}}
        out["tsum"].update(over)
        return out

    def test_order_is_created_with_the_server_amount(self):
        r = self.c.post("/run", json=self.body())
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["amount"], pricing.PRICES["tsum_coin"])
        self.assertNotIn("job", j)
        order = A._orders[j["order"]]
        self.assertEqual(order["job"]["mode"], "tsum")
        self.assertEqual(order["arguments"][0]["menus"], ["tsum_coin"])
        self.assertNotIn("a@example.com", order["summary"])
        self.assertNotIn("pw", order["summary"].split("／")[0])

    def test_two_menus_cost_two_prices(self):
        j = self.c.post("/run", json=self.body(menus=["tsum_coin", "tsum_box"])).get_json()
        self.assertEqual(j["amount"], pricing.PRICES["tsum_coin"] + pricing.PRICES["tsum_box"])

    def test_bad_input_is_refused(self):
        r = self.c.post("/run", json=self.body(qty={}))
        self.assertEqual(r.status_code, 400)
        self.assertIn("数量", r.get_json()["error"])
        r = self.c.post("/run", json=self.body(menus=[]))
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/run", json=self.body(login="09012345678"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("電話番号", r.get_json()["error"])
        self.assertFalse(A._orders)

    def test_guest_create_order_needs_no_account(self):
        r = self.c.post("/run", json=self.body(menus=["tsum_guest_create"], qty={}, login="", password=""))
        self.assertEqual(r.status_code, 200)
        order = A._orders[r.get_json()["order"]]
        self.assertEqual(order["arguments"][0]["mode"], "new")
        self.assertEqual(order["transfer_key"], None)

    def test_same_account_cannot_run_twice_at_once(self):
        j = self.c.post("/run", json=self.body()).get_json()
        key = A._orders[j["order"]]["transfer_key"]
        with patch.object(A, "_active_transfers", {key}):
            r = self.c.post("/run", json=self.body())
        self.assertEqual(r.status_code, 409)

    def test_saved_order_round_trip(self):
        j = self.c.post("/run", json=self.body()).get_json()
        order = A._orders[j["order"]]
        back = A._order_from_json(A._order_to_json(order))
        self.assertEqual(back["arguments"][0], order["arguments"][0])
        self.assertEqual(back["transfer_key"], order["transfer_key"])

    def test_nyanko_order_round_trip_is_unchanged(self):
        j = self.c.post("/run", json={"items": ["XP"], "chars": "1", "cat_settings": {"1": {"form": 2}}}).get_json()
        order = A._orders[j["order"]]
        back = A._order_from_json(A._order_to_json(order))
        self.assertEqual(back["arguments"][7], {0: {"form": 2}})

    def test_lock_subject(self):
        payload = {"mode": "line", "login": "a@example.com"}
        self.assertEqual(A._lock_subject({"mode": "tsum"}, (payload,)), "tsum:a@example.com")
        self.assertEqual(A._lock_subject({"mode": "download"}, ("abc", "1234")), "abc")
        self.assertEqual(A._lock_subject({"mode": "tsum"}, ({"mode": "new", "login": ""},)), "tsum:")

    def test_tsum_orders_are_separated_from_nyanko_orders(self):
        self.c.post("/run", json=self.body())
        self.c.post("/run", json={"items": ["XP"]})
        rows = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual(sorted(bool(o["tsum"]) for o in rows), [False, True])


class JobRun(unittest.TestCase):
    """ワーカープロセスの中の処理（tsum_service を差し替えて確かめる）。"""

    def job(self):
        with A.app.test_request_context("/tsum"):
            return A._new_tsum_job("line", ["tsum_coin"])

    def test_done_job_carries_lines_and_codes(self):
        out = {"ok": True, "error": "", "lines": ["■ コイン指定：完了"], "done": 1,
               "transfer": "ab12cd34", "confirm": "pw-9999", "moved": True}
        job = self.job()
        with patch.object(A.tsum_service, "run_order", return_value=out):
            A._run_tsum_job(job, {"mode": "guest", "login": "x", "password": "y",
                                  "menus": ["tsum_coin"], "qty": {"tsum_coin": 100}})
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"], {"transfer": "ab12cd34", "confirm": "pw-9999"})
        self.assertEqual(job["tsum"]["lines"], ["■ コイン指定：完了"])
        self.assertEqual(job["step"], A.TSUM_TOTAL_STEPS)

    def test_failed_job_reports_the_reason(self):
        out = {"ok": False, "error": "ハートが足りません", "lines": [], "done": 0,
               "transfer": None, "confirm": None, "moved": False}
        job = self.job()
        with patch.object(A.tsum_service, "run_order", return_value=out):
            A._run_tsum_job(job, {"mode": "line", "login": "x", "password": "y", "menus": ["tsum_coin"], "qty": {}})
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], "ハートが足りません")
        self.assertFalse(job["outcome_unknown"])

    def test_unexpected_error_hides_the_details(self):
        job = self.job()
        with patch.object(A.tsum_service, "run_order", side_effect=RuntimeError("password=secret-pw")):
            A._run_tsum_job(job, {"mode": "line", "login": "a@example.com", "password": "secret-pw",
                                  "menus": ["tsum_coin"], "qty": {}})
        self.assertEqual(job["status"], "error")
        self.assertIn("RuntimeError", job["error"])
        self.assertNotIn("secret-pw", job["error"])
        self.assertNotIn("secret-pw", "\n".join(job["log"]))

    def test_moved_without_a_transfer_is_marked_unknown(self):
        out = {"ok": False, "error": "発行できませんでした", "lines": [], "done": 1,
               "transfer": None, "confirm": None, "moved": True}
        job = self.job()
        with patch.object(A.tsum_service, "run_order", return_value=out):
            A._run_tsum_job(job, {"mode": "guest", "login": "x", "password": "y", "menus": ["tsum_coin"], "qty": {}})
        self.assertTrue(job["outcome_unknown"])


class Captcha(unittest.TestCase):
    def setUp(self):
        self.j = patch.dict(A._jobs, {}, clear=True)
        self.j.start()
        self.pipes = patch.dict(A._job_pipes, {}, clear=True)
        self.pipes.start()
        self.c = A.app.test_client()

    def tearDown(self):
        self.pipes.stop()
        self.j.stop()

    def test_unknown_job(self):
        r = self.c.post("/api/jobs/nope/captcha", json={"text": "AB12"})
        self.assertEqual(r.status_code, 404)

    def test_empty_text(self):
        r = self.c.post("/api/jobs/nope/captcha", json={"text": "  "})
        self.assertEqual(r.status_code, 400)

    def test_not_waiting(self):
        with self.c:
            self.c.get("/tsum")                      # このブラウザの利用者番号を作る
            job = A._new_tsum_job("line", ["tsum_coin"])
            A._jobs["j1"] = job
            r = self.c.post("/api/jobs/j1/captcha", json={"text": "AB12"})
            self.assertEqual(r.status_code, 409)

    def test_answer_is_sent_to_the_worker(self):
        sent = []
        with self.c:
            self.c.get("/tsum")
            job = A._new_tsum_job("line", ["tsum_coin"])
            job["captcha"] = {"image": "data:image/png;base64,AA", "asked": 0, "wait": 10}
            A._jobs["j2"] = job
            A._job_pipes["j2"] = types.SimpleNamespace(send=sent.append)
            r = self.c.post("/api/jobs/j2/captcha", json={"text": "ab12"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sent, [{"captcha": "ab12", "id": None}])

    def test_answer_for_an_old_image_is_refused(self):
        """画像が新しくなったあとに、前の画像への答えが届いても渡さない。"""
        sent = []
        with self.c:
            self.c.get("/tsum")
            job = A._new_tsum_job("line", ["tsum_coin"])
            job["captcha"] = {"id": "new", "image": "data:image/png;base64,AA", "asked": 0, "wait": 10}
            A._jobs["j4"] = job
            A._job_pipes["j4"] = types.SimpleNamespace(send=sent.append)
            r = self.c.post("/api/jobs/j4/captcha", json={"text": "ab12", "id": "old"})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["code"], "captcha_changed")
            ok = self.c.post("/api/jobs/j4/captcha", json={"text": "cd34", "id": "new"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(sent, [{"captcha": "cd34", "id": "new"}])

    def test_status_shows_the_captcha_and_the_result(self):
        with self.c:
            self.c.get("/tsum")
            job = A._new_tsum_job("line", ["tsum_coin"])
            job["captcha"] = {"image": "data:image/png;base64,AA", "asked": 0, "wait": 10}
            job["tsum"] = {"lines": ["■ コイン指定：完了"], "moved": False, "done": 1, "menus": ["コイン指定"]}
            A._jobs["j3"] = job
            j = self.c.get("/status/j3").get_json()
        self.assertEqual(j["captcha"]["image"], "data:image/png;base64,AA")
        self.assertEqual(j["tsum"]["lines"], ["■ コイン指定：完了"])


class Page(unittest.TestCase):
    def test_page_opens_with_every_menu(self):
        client = A.app.test_client()
        tsum = client.get("/tsum")
        self.assertEqual(tsum.status_code, 200)
        body = tsum.get_data(as_text=True)
        for key in T.MENUS:
            self.assertIn(f'data-key="{key}"', body)

    def test_main_site_asks_which_game_first(self):
        """トップページは最初に大メニュー（にゃんこ／ツムツム）を出す。ツムツムからは選び直せる。"""
        client = A.app.test_client()
        main = client.get("/").get_data(as_text=True)
        self.assertIn('id="gamePick"', main)
        self.assertIn('id="gpNyanko"', main)
        self.assertIn('href="/tsum"', main)
        self.assertLess(main.index('id="gamePick"'), main.index('id="v-start"'))   # にゃんこの画面より前
        tsum = client.get("/tsum").get_data(as_text=True)
        self.assertIn('href="/?pick=1"', tsum)


if __name__ == "__main__":
    unittest.main()


class RateLimit(unittest.TestCase):
    def test_too_many_orders_in_a_row_are_refused(self):
        with patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True), \
             patch.object(G, "save_orders"), patch.object(G, "history_upsert"), \
             patch.dict(A._hits, {}, clear=True), patch.object(A, "MAX_UNPAID_PER_OWNER", 10 ** 6):
            client = A.app.test_client()
            body = {"tsum": {"mode": "new", "login": "", "password": "",
                             "menus": ["tsum_guest_create"], "qty": {}}}
            codes = [client.post("/run", json=body).status_code for _ in range(22)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)


class WorkerCaptcha(unittest.TestCase):
    """実行プロセス側：前の画像への遅れた答えを捨て、今の画像の答えだけを使う。"""

    def test_stale_answer_is_dropped(self):
        job = {"log": [], "captcha": None}

        class FakePipe:
            def __init__(self):
                self.queue = [{"captcha": "OLD1", "id": "previous"}]   # 前回のタイムアウト後に届いた答え
                self.answered = False

            def poll(self, timeout=0):
                if self.queue:
                    return True
                if not self.answered and job.get("captcha"):
                    self.queue.append({"captcha": "OLD2", "id": "wrong"})
                    self.queue.append({"captcha": "NEW", "id": job["captcha"]["id"]})
                    self.answered = True
                    return True
                return False

            def recv(self):
                return self.queue.pop(0)

        with patch.object(A, "_worker_pipe", FakePipe()), patch.object(A, "_publish_job"):
            text = A._tsum_captcha(job, b"PNG")
        self.assertEqual(text, "NEW")
        self.assertIsNone(job["captcha"])

    def test_no_answer_returns_empty(self):
        job = {"log": [], "captcha": None}
        pipe = types.SimpleNamespace(poll=lambda timeout=0: False, recv=lambda: None)
        with patch.object(A, "_worker_pipe", pipe), patch.object(A, "_publish_job"), \
             patch.object(A, "TSUM_CAPTCHA_WAIT", 0):
            self.assertEqual(A._tsum_captcha(job, b"PNG"), "")
