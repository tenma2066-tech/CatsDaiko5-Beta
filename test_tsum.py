"""ツムツム代行のテスト（ゲーム・LINE・PayPay への通信はすべてモック）。

ここで確かめること：
- メニューの検査（数量の範囲・同時に選べない組み合わせ・実行順）
- 料金が画面とサーバーで一致すること（画面はサーバーの値を出しているだけ）
- 1回の注文の実行（メール／引き継ぎ番号／新規ゲスト。引き継ぎ情報の発行まで）
- 注文の保存・読み込み（アカウント情報の形が崩れないこと）
- 画像認証（CAPTCHA）の受け渡し
"""
import os
import types
import unittest
from unittest.mock import patch

import requests

import app as A
import site_config
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
                     "coin_max": ("coin", 200_000_000), "medal_max": ("medal", 100_000_000)},
        DEBUG_HOOK=None,
        forge_sync=forge_sync,
        Forge=lambda **kw: types.SimpleNamespace(get_public_key=lambda: "pub"),
        describe_login_failure=lambda raw: "ログインできませんでした",
        _dbg=lambda *a, **k: None,
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
        self.assertIn("範囲", T.validate_order(["tsum_medal"], {"tsum_medal": "100000001"})[2])
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

    def test_captcha_block_without_ip_rotation_is_not_retried(self):
        # 回線（送信元IP）を変えられない構成では、画像認証で弾かれても叩き直さない。
        # 同じ固定IPで繰り返しても結果は変わらず、認証の繰り返しでアカウントに負荷をかけるだけ。
        tries = []

        def login(login_id, password, **kw):
            tries.append(1)
            kw["error_box"]["error"] = "CAPTCHAが正しくないか、LINE側がもう一度画像CAPTCHAを要求しました。"
            return None

        self.core.tsum_login.headless_session_direct = login   # 既定の _unique_game_proxy は None（固定IP）
        out = T.run_order("line", "a@example.com", "pw", ["tsum_coin"], {"tsum_coin": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(len(tries), 1)
        # 利用者に実行できない・根拠のない案内を出さない
        self.assertNotIn("VPN", out["error"])
        self.assertNotIn("20〜30分", out["error"])
        self.assertNotIn("一時的に制限", out["error"])
        self.assertIn("お問い合わせ", out["error"])

    def test_captcha_block_with_ip_rotation_is_retried_on_fresh_circuit(self):
        # SOCKS プロキシ設定時は、画像認証で弾かれたら別IP（別回線）でやり直す。
        self.core._unique_game_proxy = lambda: "socks5h://ident:ident@127.0.0.1:9050"
        tries = []

        def login(login_id, password, **kw):
            tries.append(1)
            kw["error_box"]["error"] = "CAPTCHAが正しくないか、LINE側がもう一度画像CAPTCHAを要求しました。"
            return None

        self.core.tsum_login.headless_session_direct = login
        out = T.run_order("line", "a@example.com", "pw", ["tsum_coin"], {"tsum_coin": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(len(tries), 3)

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


class LoginFailureMessages(unittest.TestCase):
    """ログイン失敗の表示（describe_login_failure）。外部通信はしない。

    画像取得失敗・回答未受信・回答送信後の失敗を、それぞれ別の文言で出す。
    根拠のない「LINE側の一時制限」「20〜30分」は出さない。
    """
    @staticmethod
    def _describe():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_core
        return tsum_core.describe_login_failure

    def test_three_captcha_cases_are_distinct_and_without_speculation(self):
        describe = self._describe()
        # tsum_engine/line_password_login.py が実際に返す生の失敗理由（確認できた事実）
        image_fetch = describe("CAPTCHA画像の取得に失敗しました（通信エラー: ConnectionError: x）。もう一度お試しください。")
        no_answer = describe("CAPTCHAが入力されませんでした。")
        after_submit = describe("CAPTCHAが正しくないか、LINE側がもう一度画像CAPTCHAを要求しました。")

        self.assertIn("画像を取得できませんでした", image_fetch)
        self.assertIn("入力されなかった", no_answer)
        self.assertIn("合っていなかった", after_submit)
        self.assertEqual(len({image_fetch, no_answer, after_submit}), 3)   # 3件とも別の文言

        for msg in (image_fetch, no_answer, after_submit):
            self.assertNotIn("20〜30分", msg)
            self.assertNotIn("一時的に制限", msg)
            self.assertNotIn("制限が延びる", msg)
            self.assertNotIn("繰り返し出て", msg)
            # 診断用の内部文字列（例外名など）を画面に出さない
            self.assertNotIn("ConnectionError", msg)

    def test_game_side_failure_is_not_shown_as_wrong_password(self):
        # LINE 認証は成功したがゲーム(ツムツム)側のログインで失敗したケース。
        # LINE に「ログインしました」が届いているので、資格情報エラーと誤表示しない。
        describe = self._describe()
        m = describe("ゲーム（ツムツム）側のログインに失敗しました（コード 201）。")
        self.assertNotIn("メールアドレスかパスワード", m)
        self.assertIn("ゲーム", m)
        self.assertIn("201", m)   # 実際のコードを見せて切り分けできるようにする

    def test_recaptcha_and_other_reasons_unchanged(self):
        describe = self._describe()
        self.assertIn("reCAPTCHA", describe("CAPTCHA送信後にreCAPTCHAへ切り替わりました。別IPに変えてください。"))
        # 画像認証以外の分類はこれまで通り
        self.assertIn("PIN", describe("PIN認証がタイムアウト/未承認でした。"))
        self.assertIn("メールアドレスかパスワード", describe("アカウント情報が違う（errorCode=445）"))
        self.assertIn("BAN", describe("アカウントが停止されています（retCode=29）"))


class MyTsumSelect(unittest.TestCase):
    """ツムレベルMAX（セット中マイツム）のツム選択。通信はしない。
    セット中(setflg)が無くても、所持しているツムから選べること。"""
    @staticmethod
    def _core():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_core
        return tsum_core

    def test_prefers_set_mytsum(self):
        c = self._core()
        fake = types.SimpleNamespace(get_info=lambda: {
            "retcode": 0, "userinfo": {},
            "tsuminfo": [{"tsumid": 5, "lv": 10}, {"tsumid": 1009, "lv": 45, "setflg": 1}]})
        cands, info, err = c.select_play_tsumids(fake)
        self.assertIsNone(err)
        self.assertEqual(cands[0], 1009)   # セット中を先頭に

    def test_falls_back_to_owned_when_none_is_set(self):
        c = self._core()
        # どれも setflg なし。所持(lv>0)のツムから選べること（前回の「タイトルに戻ります」で
        # ホームが解除されていても「マイツム見つかりません」で止まらない）。
        fake = types.SimpleNamespace(get_info=lambda: {
            "retcode": 0, "userinfo": {},
            "tsuminfo": [{"tsumid": 5, "lv": 0}, {"tsumid": 1009, "lv": 45}]})
        cands, info, err = c.select_play_tsumids(fake)
        self.assertIsNone(err)
        self.assertIn(1009, cands)

    def test_errors_only_when_nothing_is_owned(self):
        c = self._core()
        fake = types.SimpleNamespace(get_info=lambda: {
            "retcode": 0, "userinfo": {}, "tsuminfo": [{"tsumid": 5, "lv": 0}]})
        cands, info, err = c.select_play_tsumids(fake)
        self.assertFalse(cands)
        self.assertTrue(err)


class LevelMaxCoins(unittest.TestCase):
    """ツムレベルMAX はコインを使う。メニュー価格に関わらず必要枚数を自動補充する。通信はしない。"""
    @staticmethod
    def _core():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_core
        return tsum_core

    def test_level_max_forces_coin_autofill_even_when_coins_are_paid(self):
        c = self._core()
        c.CONFIG["menu_prices"] = {"coin": 50}   # コインメニューは有料 → is_coin_free()=False
        try:
            self.assertFalse(c.is_coin_free())
            rec = {}

            class FakeForge:
                def resources(self, info=None):
                    return {"bcoin": 1_000_000}
                def get_info(self):
                    return {"retcode": 0, "userinfo": {}, "tsuminfo": [{"tsumid": 1024, "lv": 10}]}
                def max_tsum_level(self, tid, **kw):
                    rec["auto_fill"] = kw.get("auto_fill_coins")
                    rec["tid"] = tid
                    return {"status": "done", "lv": 50}

            res, msg = c.forge_sync(tsum_lv=1024, sess={"userid": "1"}, f=FakeForge())
            self.assertTrue(rec["auto_fill"])   # 価格が有料でもレベルMAXは強制ON
            self.assertEqual(rec["tid"], 1024)
            self.assertIn("レベルMAX", msg)
        finally:
            c.CONFIG.pop("menu_prices", None)


class NewMenus(unittest.TestCase):
    """追加メニュー（メダルMAX・指定ツム・全ツム）の配線。通信はしない。"""
    @staticmethod
    def _core():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_core
        return tsum_core

    def test_menus_and_prices_present(self):
        import pricing
        for key in ("tsum_medal_max", "tsum_tsum_lv_pick", "tsum_all_lv"):
            self.assertIn(key, T.MENUS)
            self.assertIn(T.MENUS[key]["price"], pricing.PRICES)
        # メダルMAX・全ツムは数量なし（プリセット）、指定ツムは数量（ツムID）あり
        self.assertNotIn("value", T.MENUS["tsum_medal_max"])
        self.assertNotIn("value", T.MENUS["tsum_all_lv"])
        self.assertIn("value", T.MENUS["tsum_tsum_lv_pick"])

    def test_forge_kwargs_mapping(self):
        with patch.object(T, "_core", fake_core()):
            self.assertEqual(T._forge_kwargs("tsum_medal_max", {}), {"medal": 100_000_000})
            self.assertEqual(T._forge_kwargs("tsum_tsum_lv_pick", {"tsum_tsum_lv_pick": "1024"}), {"tsum_lv": 1024})
            self.assertEqual(T._forge_kwargs("tsum_all_lv", {}), {"all_tsum_lv": True})

    def test_pick_rejects_unowned_tsum(self):
        c = self._core()

        class FakeForge:
            def resources(self, info=None):
                return {"bcoin": 1_000_000}
            def get_info(self):
                return {"retcode": 0, "userinfo": {}, "tsuminfo": [{"tsumid": 5, "lv": 10}]}
            def max_tsum_level(self, tid, **kw):
                raise AssertionError("所持していないツムで実行してはいけない")

        res, msg = c.forge_sync(tsum_lv=1024, sess={"userid": "1"}, f=FakeForge())
        self.assertIsNone(res)
        self.assertIn("所持していません", msg)

    def test_all_tsum_lv_loops_owned_and_forces_autofill(self):
        c = self._core()
        calls = []

        class FakeForge:
            def get_info(self):
                return {"retcode": 0, "userinfo": {},
                        "tsuminfo": [{"tsumid": 5, "lv": 10}, {"tsumid": 1024, "lv": 20}]}
            def max_tsum_level(self, tid, **kw):
                calls.append((tid, kw.get("auto_fill_coins")))
                return {"status": "done", "lv": 50}

        res, msg = c.forge_sync(all_tsum_lv=True, sess={"userid": "1"}, f=FakeForge())
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["done"], 2)
        self.assertTrue(all(af is True for _, af in calls))   # 全ツムもコイン自動補充
        self.assertEqual({tid for tid, _ in calls}, {5, 1024})

    def test_all_tsum_lv_stops_on_heart_out_and_reports_partial(self):
        c = self._core()

        class FakeForge:
            def get_info(self):
                return {"retcode": 0, "userinfo": {},
                        "tsuminfo": [{"tsumid": 5, "lv": 10}, {"tsumid": 1024, "lv": 20}]}
            def max_tsum_level(self, tid, **kw):
                if tid == 5:
                    return {"status": "done", "lv": 50}
                return {"status": "heart_ran_out", "lv": 25}

        res, msg = c.forge_sync(all_tsum_lv=True, sess={"userid": "1"}, f=FakeForge())
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["done"], 1)
        self.assertIn("ハート", msg)


class NicknameMenu(unittest.TestCase):
    """ニックネーム変更（文字入力メニュー）。通信はしない。"""
    @staticmethod
    def _core():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_core
        return tsum_core

    def test_validate_nickname(self):
        menus, qty, err = T.validate_order(["tsum_nickname"], {"tsum_nickname": "  たろう  "})
        self.assertEqual(err, "")
        self.assertEqual(qty["tsum_nickname"], "たろう")   # 前後の空白は落とす
        self.assertIn("文字を入力", T.validate_order(["tsum_nickname"], {"tsum_nickname": "   "})[2])
        self.assertIn("文字", T.validate_order(["tsum_nickname"], {"tsum_nickname": "あ" * 17})[2])
        self.assertIn("使えない", T.validate_order(["tsum_nickname"], {"tsum_nickname": "a\nb"})[2])

    def test_forge_kwargs(self):
        with patch.object(T, "_core", fake_core()):
            self.assertEqual(T._forge_kwargs("tsum_nickname", {"tsum_nickname": "ネオ"}), {"nickname": "ネオ"})

    def test_forge_sync_sets_nickname(self):
        c = self._core()
        rec = {}

        class FakeForge:
            def set_profile(self, hometsum=None, trophyid=0, nickname=None):
                rec["nick"] = nickname
                return {"retcode": 0}

        res, msg = c.forge_sync(nickname="ネオ", sess={"userid": "1"}, f=FakeForge())
        self.assertEqual(rec["nick"], "ネオ")
        self.assertIn("ネオ", msg)

    def test_forge_sync_nickname_failure_is_reported(self):
        c = self._core()

        class FakeForge:
            def set_profile(self, hometsum=None, trophyid=0, nickname=None):
                return {"retcode": 5, "retmsg": "だめ"}

        res, msg = c.forge_sync(nickname="x", sess={"userid": "1"}, f=FakeForge())
        self.assertIsNone(res)
        self.assertTrue(msg)


class GameSync(unittest.TestCase):
    """ゲーム内アクションのデータ同期ずれ(retcode 22)からの復旧。通信はしない。

    プレイ報酬でコインが変わった直後の上限解放などで、サーバーが
    「コイン残数が一致しません。データ反映のため一度タイトルに戻ります。」(retcode 22)
    を返す。これはセッション切れ(retcode 6)と同じく「取り直して再試行せよ」の意なので、
    post() が再ログイン＋1回やり直す。
    """
    @staticmethod
    def _forge_cls():
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_engine"))
        import tsum_forge
        return tsum_forge.Forge

    def test_retcode22_coin_desync_is_classified_as_resync(self):
        F = self._forge_cls()
        self.assertTrue(F._needs_session_resync(
            {"retcode": 22, "retsubcode": 2, "retmsg": "コイン残数が一致しません。データ反映のため一度タイトルに戻ります。"}))
        self.assertTrue(F._needs_session_resync({"retcode": 6}))
        self.assertFalse(F._needs_session_resync({"retcode": 0}))
        self.assertFalse(F._needs_session_resync({"retcode": 22, "retsubcode": 9, "retmsg": "同期と無関係なエラー"}))

    def test_post_resyncs_and_retries_on_retcode22(self):
        F = self._forge_cls()
        fg = F(userid="1", hashv="h" * 8, checkval="cv", proxy=None, user_token="t")
        calls = []
        responses = [
            {"retcode": 22, "retsubcode": 2, "retmsg": "コイン残数が一致しません。データ反映のため一度タイトルに戻ります。"},
            {"retcode": 0, "ok": True},
        ]
        def fake_post_once(path, pairs):
            calls.append(path)
            return responses[min(len(calls) - 1, len(responses) - 1)]
        fg._post_once = fake_post_once
        fg.refresh_session = lambda: True
        out = fg.post("releaseTsumLv.nhn", [("tsumid", 1009)])
        self.assertEqual(out.get("retcode"), 0)
        self.assertEqual(len(calls), 2)   # 1回目22 → 再ログイン → 2回目で成功

    def test_post_does_not_loop_if_resync_still_fails(self):
        F = self._forge_cls()
        fg = F(userid="1", hashv="h" * 8, checkval="cv", proxy=None, user_token="t")
        calls = []
        def fake_post_once(path, pairs):
            calls.append(path)
            return {"retcode": 22, "retsubcode": 2, "retmsg": "コイン残数が一致しません。"}
        fg._post_once = fake_post_once
        fg.refresh_session = lambda: True
        out = fg.post("releaseTsumLv.nhn", [("tsumid", 1009)])
        self.assertEqual(out.get("retcode"), 22)   # 2回目も22ならそのまま返す（無限ループしない）
        self.assertEqual(len(calls), 2)


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
        # 新しいアカウントを作る注文（ログインIDなし）は、共通の鍵を持たない＝作成の注文どうしが締め出し合わない
        self.assertEqual(A._lock_subject({"mode": "tsum"}, ({"mode": "new", "login": ""},)), "")
        self.assertEqual(A._lock_subject({"mode": "puni"}, ({"mode": "new", "login": ""},)), "")

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

    def test_home_is_the_big_menu_and_each_game_is_its_own_page(self):
        """トップ（/）が大メニュー。にゃんこ（/nyanko）とツムツム（/tsum）は別のページなので、
        ブラウザの戻るボタンで素直に行き来できる（重ねて出す方式は戻るでおかしくなった）。"""
        client = A.app.test_client()
        home = client.get("/").get_data(as_text=True)
        self.assertIn('href="/nyanko"', home)
        self.assertIn('href="/tsum"', home)
        self.assertNotIn('id="v-start"', home)              # にゃんこの画面そのものは入っていない
        nyanko = client.get("/nyanko").get_data(as_text=True)
        self.assertIn('id="v-start"', nyanko)
        self.assertNotIn('id="gamePick"', nyanko)            # 重ねて出す大メニューはもう無い
        self.assertIn('href="/"', nyanko)
        tsum = client.get("/tsum").get_data(as_text=True)
        self.assertIn('href="/"', tsum)

    def test_tsum_page_can_resume_a_paid_order_by_number(self):
        """ツムツムの画面に「注文番号で再開」の入口がある（別端末・画面を閉じた・画像認証で止まった人向け）。
        バックエンドの /api/orders/<id>/claim は test_order_flow で担保。ここは画面側の導線が消えないよう固定する。"""
        tsum = A.app.test_client().get("/tsum").get_data(as_text=True)
        self.assertIn('id="claimNo"', tsum)
        self.assertIn('id="claimGo"', tsum)
        # 再開の処理そのものは、ツムツム・ぷにぷに共通の static/shop.js にある（画面はそれを読み込む）
        self.assertIn('src="/static/shop.js', tsum)
        shop_js = A.app.test_client().get("/static/shop.js").get_data(as_text=True)
        self.assertIn('"/api/orders/" + encodeURIComponent(no) + "/claim"', shop_js)
        # にゃんこ側にも元からある（両方の画面で再開できる）
        nyanko = A.app.test_client().get("/nyanko").get_data(as_text=True)
        self.assertIn('id="claimNo"', nyanko)

    def test_old_entry_redirects_to_nyanko(self):
        client = A.app.test_client()
        r = client.get("/?game=nyanko")
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r.headers["Location"].endswith("/nyanko"))
        self.assertIn("location.replace('/nyanko')", client.get("/").get_data(as_text=True))   # /#nyanko


class Images(unittest.TestCase):
    """画像（大メニュー・ツムツム画面・X のリンクプレビュー・アイコン）がそろっていて、画面から参照されていること。"""

    FILES = ["game-nyanko.webp", "game-nyanko.jpg", "game-tsum.webp", "game-tsum.jpg",
             "ogp-home.jpg", "favicon.ico", "icon-192.png", "icon-512.png", "apple-touch-icon.png"]
    # 店名や連絡先が描き込まれた画像。お店ごとに用意するもので、無くても画面は成り立つ（site_config.py）
    SHOP_BRANDED = ["tsum-flow.webp", "tsum-flow.jpg", "flow.webp", "flow.jpg", "ogp-nyanko.jpg", "ogp-tsum.jpg"]

    def test_files_are_served(self):
        client = A.app.test_client()
        static = os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "static")
        present = [n for n in self.SHOP_BRANDED if os.path.isfile(os.path.join(static, n))]
        for name in self.FILES + present:
            r = client.get("/static/" + name)
            self.assertEqual(r.status_code, 200, name)
            self.assertGreater(len(r.data), 1000, name)
            r.close()
        r = client.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_link_preview_uses_https_behind_caddy(self):
        client = A.app.test_client()
        headers = {"X-Forwarded-Proto": "https", "Host": "shop.example"}
        home = client.get("/", headers=headers).get_data(as_text=True)
        self.assertIn('content="https://shop.example/static/ogp-home.jpg?v=1"', home)
        self.assertIn("game-nyanko.webp", home)
        self.assertIn("game-tsum.webp", home)
        main = client.get("/nyanko", headers=headers).get_data(as_text=True)
        og = site_config.og_images()   # お店用の画像があればそれ、無ければゲームの絵
        self.assertIn(f'content="https://shop.example{og["nyanko"]["src"]}"', main)
        self.assertIn('content="https://shop.example/nyanko"', main)
        tsum = client.get("/tsum", headers=headers).get_data(as_text=True)
        self.assertIn(f'content="https://shop.example{og["tsum"]["src"]}"', tsum)
        self.assertIn('content="https://shop.example/tsum"', tsum)
        self.assertEqual("tsum-flow.webp" in tsum, site_config.flow_images()["tsum"])   # 図解は、画像があるときだけ出す
        for page in (home, main, tsum):
            self.assertIn('rel="apple-touch-icon"', page)
            self.assertIn('name="twitter:card" content="summary_large_image"', page)


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



class PremiumBoxPlus(unittest.TestCase):
    """プレミアムBOX+（メダルで引く）：メニューの出し方、ガチャの見つけ方、引き方"""

    def _core(self):
        return NewMenus._core()

    def _forge(self, c, rows, result, probe=None):
        seen = {"ids": [], "paytype": None}
        probe = probe or {"status": "not_found", "tried": []}

        class FakeForge:
            def get_mast(self_):
                return {"retcode": 0, "gachamst": rows}
            def gacha_compflg(self_, gid, info=None):
                return 0
            def probe_medal_paytype(self_, gid, cands, **kw):
                seen["probe"] = list(cands)
                return dict(probe)
            def draw_with_medals(self_, gid, paytype, **kw):
                seen["ids"].append(gid); seen["paytype"] = paytype
                return dict(result)
        c._GACHA_CACHE.update(ts=0.0, ids=None)
        return FakeForge(), seen

    def _run(self, c, f, paytype="3"):
        with patch.dict(os.environ, {"TSUM_PREMIUM_PLUS_PAYTYPE": paytype}):
            return c.forge_sync(gacha_plus=0, sess={"userid": "1"}, f=f, auto_fill_coins=False)

    def test_menu_price_and_kwargs(self):
        self.assertIn("tsum_premium_plus", T.MENUS)
        self.assertIn("tsum_premium_plus", pricing.PRICES)
        with patch.object(T, "_core", fake_core()):
            self.assertEqual(T._forge_kwargs("tsum_premium_plus", {}), {"gacha_plus": 0})

    def test_name_match(self):
        c = self._core()
        for name in ("プレミアムBOX+", "プレミアムＢＯＸ＋", "プレミアムbox +", "プレミアムBOXプラス"):
            self.assertTrue(c._is_box_plus_name(name), name)
        self.assertFalse(c._is_box_plus_name("プレミアムBOX"))

    def test_found_by_name_before_type_and_paytype_passed(self):
        c = self._core()
        rows = [{"gachaid": 11, "type": 11, "name": "プレミアムBOX"},
                {"gachaid": 64, "type": 64, "name": "ハピネスBOX"},
                {"gachaid": 77, "type": 70, "name": "プレミアムBOX+"}]
        f, seen = self._forge(c, rows, {"status": "complete", "draws": 120, "medal": 5})
        res, msg = self._run(c, f)
        self.assertEqual(seen, {"ids": [77], "paytype": 3})
        self.assertIn("最後まで引きました（120連）", msg)
        self.assertIn("残りメダル: 5枚", msg)

    def _no_saved(self, c, tmp):
        return patch.object(c, "_PAYTYPE_FILE", os.path.join(tmp, "pt.json"))

    def test_probe_finds_paytype_saves_it_and_counts_the_draw(self):
        import tempfile
        c = self._core()
        with tempfile.TemporaryDirectory() as tmp, self._no_saved(c, tmp):
            f, seen = self._forge(c, [{"gachaid": 64, "type": 64}], {"status": "stopped", "draws": 20, "medal": 0},
                                  probe={"status": "found", "paytype": 4, "medal": 90, "tried": []})
            res, msg = self._run(c, f, paytype="")
            self.assertEqual(seen["paytype"], 4)
            self.assertNotIn(1, seen["probe"])          # コイン払いの 1 は試さない
            self.assertIn("引けるところまで引きました（21連）", msg)
            self.assertEqual(c.premium_plus_paytype(), 4)   # 次からは保存した番号を使う

    def test_probe_refuses_on_accounts_with_rubies(self):
        import tempfile
        c = self._core()
        with tempfile.TemporaryDirectory() as tmp, self._no_saved(c, tmp):
            f, seen = self._forge(c, [{"gachaid": 64, "type": 64}], {"status": "complete", "draws": 1},
                                  probe={"status": "has_ruby", "tried": []})
            res, msg = self._run(c, f, paytype="")
            self.assertIsNone(res)
            self.assertEqual(seen["ids"], [])
            self.assertIn("ルビーを1個も持っていない", msg)

    def test_type_fallback_and_not_running(self):
        c = self._core()
        f, seen = self._forge(c, [{"gachaid": 11, "type": 11}, {"gachaid": 64, "type": 64}],
                              {"status": "complete", "draws": 10})
        self._run(c, f)
        self.assertEqual(seen["ids"], [64])
        f, seen = self._forge(c, [{"gachaid": 11, "type": 11}], {})
        res, msg = self._run(c, f)
        self.assertIsNone(res)
        self.assertIn("開催されていない", msg)

    def test_results(self):
        c = self._core()
        cases = [({"status": "stopped", "draws": 33, "medal": 2}, True, "メダルで引けるところまで引きました（33連）"),
                 ({"status": "stopped", "draws": 0, "medal": 2, "resp": "x"}, False, "1回も引けませんでした"),
                 ({"status": "wrong_currency", "draws": 1}, False, "メダル以外で支払われた"),
                 ({"status": "already_complete", "draws": 0, "medal": 9}, True, "すでに最後まで")]
        for result, ok, text in cases:
            f, _ = self._forge(c, [{"gachaid": 64, "type": 64}], result)
            res, msg = self._run(c, f)
            self.assertEqual(res is not None, ok, result)
            self.assertIn(text, msg)


class DrawWithMedals(unittest.TestCase):
    """Forge.draw_with_medals を、メダルで引く偽のガチャ相手に動かす"""

    def _forge(self, medal, cost, stock, coin_cost=0, ruby_cost=0):
        Forge = NewMenus._core().Forge
        st = {"medal": medal, "coin": 1000, "ruby": 50, "stock": stock, "calls": 0}

        class Fake:
            _WALLET_KEYS = Forge._WALLET_KEYS
            _wallet = Forge.__dict__["_wallet"].__get__(None, Forge)
            draw_with_medals = Forge.draw_with_medals
            def _ui(self_):
                return {"userinfo": {"bmedal": st["medal"], "bcoin": st["coin"], "bruby": st["ruby"], "pruby": 0},
                        "gachainfo": [{"gachaid": 9, "compflg": 1 if st["stock"] == 0 else 0}]}
            def get_info(self_):
                return self_._ui()
            def gacha_compflg(self_, gid, info=None):
                return 1 if st["stock"] == 0 else 0
            def refresh_session(self_):
                return False
            def _draw(self_, n):
                st["calls"] += 1
                if st["stock"] < n or st["medal"] < cost * n:
                    return {"retcode": 99}
                st["stock"] -= n; st["medal"] -= cost * n
                st["coin"] -= coin_cost * n; st["ruby"] -= ruby_cost * n
                return dict(retcode=0, **self_._ui())
            def gacha_multi(self_, gid, paytype):
                return self_._draw(10)
            def gacha_single(self_, gid, paytype):
                return self_._draw(1)
        return Fake(), st

    def test_until_medals_run_out(self):
        f, st = self._forge(medal=2500, cost=100, stock=1000)
        r = f.draw_with_medals(9, 3, log=None)
        self.assertEqual((r["status"], r["draws"], r["medal"]), ("stopped", 25, 0))

    def test_until_box_is_empty(self):
        f, st = self._forge(medal=10**6, cost=100, stock=23)
        r = f.draw_with_medals(9, 3, log=None)
        self.assertEqual((r["status"], r["draws"]), ("complete", 23))

    def test_stops_after_one_draw_if_coins_were_spent(self):
        f, st = self._forge(medal=10**6, cost=0, stock=100, coin_cost=10)
        r = f.draw_with_medals(9, 1, log=None)
        self.assertEqual(r["status"], "wrong_currency")
        self.assertEqual(st["calls"], 1)

    def test_probe(self):
        f, st = self._forge(medal=1000, cost=100, stock=100)
        st["ruby"] = 0
        # 2 はコインで払われる番号、3 はメダル、という偽のサーバー
        orig = f._draw
        def single(gid, pt):
            if pt == 2:
                st["coin"] -= 30
                return dict(retcode=0, **f._ui())
            if pt == 3:
                return orig(1)
            return {"retcode": 99}
        f.gacha_single = single
        f.probe_medal_paytype = NewMenus._core().Forge.probe_medal_paytype.__get__(f)
        r = f.probe_medal_paytype(9, (5, 2, 3), log=None)
        self.assertEqual((r["status"], r["paytype"], r["medal"]), ("found", 3, 900))
        self.assertEqual([t["result"] for t in r["tried"]], ["rejected", "drawn", "drawn"])
        st["ruby"] = 1
        self.assertEqual(f.probe_medal_paytype(9, (3,), log=None)["status"], "has_ruby")

    def test_stops_after_one_draw_if_rubies_were_spent(self):
        f, st = self._forge(medal=10**6, cost=100, stock=100, ruby_cost=1)
        r = f.draw_with_medals(9, 2, log=None)
        self.assertEqual(r["status"], "wrong_currency")
        self.assertEqual(st["calls"], 1)
