"""ぷにぷに代行（puni_service / pricing.compute_puni_amount）の単体テスト。
tsum 側の test_tsum.py と同じ型で、画面・PayPay は触らずに検査だけ回す。"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

import pricing
import puni_service


class ValidateOrder(unittest.TestCase):
    def test_empty_menus(self):
        _, _, _, err = puni_service.validate_order([], {}, {})
        self.assertIn("メニュー", err)

    def test_solo_cannot_combine(self):
        _, _, _, err = puni_service.validate_order(
            ["puni_account", "puni_farm"], {"puni_account": "1", "puni_farm": "5"},
            {"puni_farm": "29704006"})
        self.assertIn("同時に選べません", err)

    def test_farm_needs_stage(self):
        _, _, _, err = puni_service.validate_order(
            ["puni_farm"], {"puni_farm": "5"}, {})
        self.assertIn("ステージID", err)

    def test_unlimited_is_hours_and_bounded(self):
        # P-09：入力は時間単位（1〜6）。範囲外は弾く。
        _, _, _, err = puni_service.validate_order(
            ["puni_unlimited"], {"puni_unlimited": "7"},
            {"puni_unlimited": "29704006"})
        self.assertIn("1〜6", err)
        m, q, s, err = puni_service.validate_order(
            ["puni_unlimited"], {"puni_unlimited": "3"},
            {"puni_unlimited": "29704006"})
        self.assertEqual(err, "")
        self.assertEqual(q["puni_unlimited"], 3)

    def test_stage_clear_has_no_quantity_field(self):
        # P-07：ステージクリアは数量欄を持たず、ステージIDだけで通る（二重入力解消）。
        self.assertNotIn("value", puni_service.MENUS["puni_stage_clear"])
        _, q, s, err = puni_service.validate_order(
            ["puni_stage_clear"], {}, {"puni_stage_clear": "29704006"})
        self.assertEqual(err, "")
        self.assertEqual(q, {})
        self.assertEqual(s, {"puni_stage_clear": 29704006})

    def test_stage_id_range(self):
        _, _, _, err = puni_service.validate_order(
            ["puni_stage_clear"], {}, {"puni_stage_clear": "999"})
        self.assertIn("ステージID", err)

    def test_good_order(self):
        m, q, s, err = puni_service.validate_order(
            ["puni_farm", "puni_hitodama"],
            {"puni_farm": "25", "puni_hitodama": "500"},
            {"puni_farm": "29704006"})
        self.assertEqual(err, "")
        self.assertEqual(m, ["puni_farm", "puni_hitodama"])
        self.assertEqual(q, {"puni_farm": 25, "puni_hitodama": 500})
        self.assertEqual(s, {"puni_farm": 29704006})

    def test_order_is_sorted(self):
        m, _, _, _ = puni_service.validate_order(
            ["puni_hitodama", "puni_coin_gacha", "puni_farm"],
            {"puni_farm": "10", "puni_hitodama": "500"},
            {"puni_farm": "29704006"})
        self.assertEqual(m, ["puni_farm", "puni_hitodama", "puni_coin_gacha"])


class CheckAccount(unittest.TestCase):
    def test_email_needs_both(self):
        self.assertIn("入力", puni_service.check_account("email", "a@b.com", ""))
        self.assertIn("入力", puni_service.check_account("email", "", "x"))

    def test_email_needs_at(self):
        self.assertIn("メールアドレス", puni_service.check_account("email", "no-at-sign", "x"))

    def test_email_ok(self):
        self.assertEqual(puni_service.check_account("email", "a@b.com", "x"), "")

    def test_new_skips_credentials(self):
        self.assertEqual(puni_service.check_account("new", "", ""), "")

    def test_bad_mode(self):
        self.assertIn("入力方法", puni_service.check_account("xxx", "a@b.com", "x"))


class EmailValidation(unittest.TestCase):
    """P-17：メールアドレス検証の拡充。"""

    def test_accepts_normal_addresses(self):
        for e in ("name@example.com", "a.b+c@sub.example.co.jp", "x_y@mail-server.io"):
            self.assertTrue(puni_service.valid_email(e), e)

    def test_rejects_malformed(self):
        for e in ("", "no-at", "a@b", "a@b.c", "a b@c.com", "a@@b.com",
                  "a..b@c.com", "a@b..com", "x" * 260 + "@a.com",
                  "a@b.com\n", "a@b.com ", " a@b.com"):
            self.assertFalse(puni_service.valid_email(e), e)


class PasswordQuality(unittest.TestCase):
    """P-14：暗号的に安全な乱数＋文字種保証。"""

    def test_length_and_classes(self):
        for _ in range(50):
            pw = puni_service._gen_password()
            self.assertGreaterEqual(len(pw), 8)
            self.assertTrue(any(c.islower() for c in pw))
            self.assertTrue(any(c.isupper() for c in pw))
            self.assertTrue(any(c.isdigit() for c in pw))

    def test_uses_secrets_module(self):
        import inspect
        src = inspect.getsource(puni_service._gen_password)
        self.assertIn("secrets", src)
        self.assertNotIn("random.choice", src)


class ComputePuniAmount(unittest.TestCase):
    def test_farm_rounds_up_per_10(self):
        amt, rows = pricing.compute_puni_amount(["puni_farm"], {"puni_farm": 25})
        # 25周 → 10周×3件分 → 50円×3 = 150
        self.assertEqual(amt, 3 * pricing.PRICES["puni_farm_10"])
        self.assertEqual(rows[0][1], 3)

    def test_unlimited_bills_per_hour(self):
        # P-09：入力時間（時間単位）と課金単位が一致する。3時間 → 3件。
        amt, rows = pricing.compute_puni_amount(["puni_unlimited"], {"puni_unlimited": 3})
        self.assertEqual(amt, 3 * pricing.PRICES["puni_hour"])
        self.assertEqual(rows[0][1], 3)

    def test_account_multiplies_by_count(self):
        amt, _ = pricing.compute_puni_amount(["puni_account"], {"puni_account": 5})
        self.assertEqual(amt, 5 * pricing.PRICES["puni_account"])

    def test_hitodama_is_flat(self):
        amt, _ = pricing.compute_puni_amount(["puni_hitodama"], {"puni_hitodama": 500})
        self.assertEqual(amt, pricing.PRICES["puni_hitodama"])


class MenusTable(unittest.TestCase):
    def test_all_menus_have_price_entry(self):
        for key, menu in puni_service.MENUS.items():
            self.assertIn(menu["price"], pricing.PRICES,
                          f"{key} の料金キー {menu['price']} が PRICES に無い")

    def test_solo_menus_exist(self):
        for key in puni_service.SOLO_MENUS:
            self.assertIn(key, puni_service.MENUS)

    def test_stage_menus_exist(self):
        for key in puni_service.STAGE_MENUS:
            self.assertIn(key, puni_service.MENUS)


class RunOrderDispatch(unittest.TestCase):
    """run_order の筋道（ネットワークには触らず、engine をスタブして分岐だけ確かめる）。"""

    def _fake_core(self):
        return types.SimpleNamespace(
            requests=mock.MagicMock(), VipUnavailable=RuntimeError,
            create_save=mock.MagicMock(), link_email=mock.MagicMock(),
            save_udkey_for=mock.MagicMock(), login_email_prepared=mock.MagicMock())

    def test_empty_menus_errors(self):
        out = puni_service.run_order("email", "a@b.c", "x", [], {}, {})
        self.assertFalse(out["ok"])
        self.assertIn("メニュー", out["error"])

    def test_login_failure_is_reported_gracefully(self):
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", side_effect=RuntimeError("bad creds")):
            out = puni_service.run_order(
                "email", "a@b.c", "x", ["puni_hitodama"], {"puni_hitodama": 500}, {})
        self.assertFalse(out["ok"])
        self.assertIn("ログイン", out["error"])
        self.assertIsNone(out["account"])

    def test_action_menu_runs_after_login(self):
        client = object()
        calls = []

        def fake_action(c_mod, cli, menu, qty, stages, prog, log, *more):
            calls.append(menu)
            return f"■ {puni_service.menu_label(menu)}：完了"

        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", return_value=client), \
             mock.patch.object(puni_service, "_run_action", side_effect=fake_action):
            out = puni_service.run_order(
                "email", "a@b.c", "x", ["puni_farm", "puni_hitodama"],
                {"puni_farm": 10, "puni_hitodama": 500}, {"puni_farm": 29704006})
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(out["done"], 2)
        self.assertEqual(calls, ["puni_farm", "puni_hitodama"])

    def test_action_failure_stops_and_reports(self):
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", return_value=object()), \
             mock.patch.object(puni_service, "_run_action", side_effect=RuntimeError("rc=202")):
            out = puni_service.run_order(
                "email", "a@b.c", "x", ["puni_stage_clear"],
                {"puni_stage_clear": 29704006}, {"puni_stage_clear": 29704006})
        self.assertFalse(out["ok"])
        self.assertIn("rc=202", out["error"])
        self.assertTrue(out["lines"])

    def test_new_mode_creates_accounts(self):
        def fake_create(c_mod, menu, count, prog, log):
            accs = [{"email": f"u{i}@x.com", "password": "pw", "player_name": "ぷに1"}
                    for i in range(count)]
            return accs, ""

        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_create_accounts", side_effect=fake_create):
            out = puni_service.run_order(
                "new", "", "", ["puni_account"], {"puni_account": 2}, {})
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(len(out["account"]), 2)
        self.assertEqual(out["done"], 2)

    def test_solo_menu_forces_account_path(self):
        """mode=email でも puni_aged が先頭なら作成系に流れる。"""
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_create_accounts",
                               return_value=([{"email": "a@x.com", "password": "p",
                                               "player_name": "ぷに1", "cleared": True}], "")) as cre, \
             mock.patch.object(puni_service, "_login") as login:
            out = puni_service.run_order(
                "email", "a@b.c", "x", ["puni_aged"], {"puni_aged": 1}, {})
        self.assertTrue(out["ok"])
        cre.assert_called_once()
        login.assert_not_called()


class PartialDelivery(unittest.TestCase):
    """P-02：一部でも提供したらフラグを立て、自動再実行させない。"""

    def _fake_core(self):
        return types.SimpleNamespace(
            requests=mock.MagicMock(), VipUnavailable=RuntimeError,
            create_save=mock.MagicMock(), link_email=mock.MagicMock(),
            save_udkey_for=mock.MagicMock(), login_email_prepared=mock.MagicMock())

    def test_partial_marks_delivered(self):
        def partial(*a, **k):
            raise puni_service._ActionPartial("3/10 周で中断：rc=202", 3)
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", return_value=object()), \
             mock.patch.object(puni_service, "_run_action", side_effect=partial):
            out = puni_service.run_order(
                "email", "a@b.com", "x", ["puni_farm"],
                {"puni_farm": 10}, {"puni_farm": 29704006})
        self.assertFalse(out["ok"])
        self.assertTrue(out["delivered"])
        self.assertIn("一部完了", out["lines"][0])

    def test_total_failure_not_delivered(self):
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", return_value=object()), \
             mock.patch.object(puni_service, "_run_action", side_effect=RuntimeError("rc=202")):
            out = puni_service.run_order(
                "email", "a@b.com", "x", ["puni_stage_clear"],
                {}, {"puni_stage_clear": 29704006})
        self.assertFalse(out["ok"])
        self.assertFalse(out["delivered"])

    def test_login_failure_not_delivered(self):
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_login", side_effect=RuntimeError("bad")):
            out = puni_service.run_order(
                "email", "a@b.com", "x", ["puni_farm"],
                {"puni_farm": 5}, {"puni_farm": 29704006})
        self.assertFalse(out["delivered"])

    def test_accounts_partial_marks_delivered(self):
        def one_then_fail(c_mod, menu, count, prog, log):
            return [{"email": "a@x.com", "password": "p", "player_name": "ぷに1"}], "2件目失敗"
        with mock.patch.object(puni_service, "core", self._fake_core), \
             mock.patch.object(puni_service, "_create_accounts", side_effect=one_then_fail):
            out = puni_service.run_order("new", "", "", ["puni_account"], {"puni_account": 2}, {})
        self.assertFalse(out["ok"])        # 2件目失敗
        self.assertTrue(out["delivered"])  # 1件は作成済み → 再実行させない


class BattleCodes(unittest.TestCase):
    """_battle_once のリザルトコード処理（P-10 ほか）。"""

    def test_success_returns(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        client.battle.return_value = (0, {})
        puni_service._battle_once(c, client, 29704006, lambda _t: None)  # 例外なし

    def test_version_code_raises_update_message(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        client.battle.return_value = (20, {})
        with self.assertRaises(RuntimeError) as cm:
            puni_service._battle_once(c, client, 29704006, lambda _t: None)
        self.assertIn("バージョン", str(cm.exception))

    def test_fatal_code_raises(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        client.battle.return_value = (202, {})
        with self.assertRaises(RuntimeError):
            puni_service._battle_once(c, client, 29704006, lambda _t: None)

    def test_hitodama_shortage_tries_refill_then_succeeds(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        # 1回目：ひとだま不足、補充後2回目成功
        client.battle.side_effect = [(5, {"dialogMsg": "ひとだまが足りません"}), (0, {})]
        client.buy_hitodama.return_value = 500
        puni_service._battle_once(c, client, 29704006, lambda _t: None)
        client.buy_hitodama.assert_called_once()


class NewMenus(unittest.TestCase):
    """追加メニュー（通常ガチャ・ミッション・プレゼント・最強編成・レベル解放）の実行分岐。"""

    def _run(self, menu, client):
        prog = puni_service._Progress(None)
        return puni_service._run_action(mock.MagicMock(), client, menu, {}, {}, prog, lambda _t: None)

    def test_normal_coin_gacha(self):
        c = mock.MagicMock()
        c.pull_coin_gachas.return_value = [("c1", "g1", [1, 2]), ("c1", "g1", [3])]
        line = self._run("puni_coin_gacha_normal", c)
        self.assertIn("完了", line)
        self.assertIn("3 体", line)   # 2+1 体

    def test_missions(self):
        c = mock.MagicMock()
        c.receive_missions.return_value = [1, 2, 3, 4]
        self.assertIn("4 件", self._run("puni_missions", c))

    def test_presents(self):
        c = mock.MagicMock()
        c.receive_presents.return_value = [{"seq": 1}, {"seq": 2}]
        self.assertIn("2 件", self._run("puni_presents", c))

    def test_deck_already_strongest(self):
        c = mock.MagicMock()
        c.sort_deck.return_value = ("same", ["1", "2"], ["1", "2"])
        self.assertIn("すでに最強", self._run("puni_deck", c))

    def test_deck_changed(self):
        c = mock.MagicMock()
        c.sort_deck.return_value = (0, ["1"], ["9", "8", "7"])
        self.assertIn("3 体を編成", self._run("puni_deck", c))

    def test_deck_failure_raises(self):
        c = mock.MagicMock()
        c.sort_deck.return_value = (4, ["1"], ["2"])
        with self.assertRaises(RuntimeError):
            self._run("puni_deck", c)

    def test_level_open(self):
        c = mock.MagicMock()
        c.current_deck.return_value = (["10", "20"], {})
        c.youkai.return_value = [{"id": 10, "raw": ["10", "9"]}, {"id": 20, "raw": ["20", "50"]}]
        c.open_level.side_effect = [("ok", 10), ("skip:上限", 0)]
        line = self._run("puni_level_open", c)
        self.assertIn("1 体解放", line)
        self.assertIn("10", line)

    def test_level_open_none(self):
        c = mock.MagicMock()
        c.current_deck.return_value = (["10"], {})
        c.youkai.return_value = [{"id": 10, "raw": ["10", "50"]}]
        c.open_level.return_value = ("skip:上限", 0)
        self.assertIn("解放できる段はありません", self._run("puni_level_open", c))

    def test_all_new_menus_priced(self):
        import pricing
        for k in ("puni_coin_gacha_normal", "puni_missions", "puni_presents", "puni_deck", "puni_level_open"):
            self.assertIn(k, puni_service.MENUS)
            self.assertIn(puni_service.MENUS[k]["price"], pricing.PRICES)


class StageExistence(unittest.TestCase):
    """P-08：ステージIDのマスタ照合。"""

    def test_unknown_stage_is_false(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        client.master_table.return_value = [["29704006", "x"], ["1001001", "y"]]
        self.assertTrue(puni_service._stage_exists(c, client, 29704006))
        self.assertFalse(puni_service._stage_exists(c, client, 12345678))

    def test_master_unavailable_does_not_block(self):
        c = mock.MagicMock()
        client = mock.MagicMock()
        client.master_table.side_effect = RuntimeError("no master")
        self.assertTrue(puni_service._stage_exists(c, client, 12345678))


class CommsSplit(unittest.TestCase):
    """通信の分離（ツムツムに合わせる）。"""

    def test_login_proxy_from_env(self):
        with mock.patch.dict(os.environ, {"PUNI_SOCKS_URL": "socks5://127.0.0.1:9050"}, clear=False):
            self.assertEqual(puni_service._login_proxy(), "socks5://127.0.0.1:9050")

    def test_game_api_direct_by_default(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PUNI_TOR_GAME_API",)}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["PUNI_SOCKS_URL"] = "socks5://127.0.0.1:9050"
            self.assertIsNone(puni_service._game_api_proxy())

    def test_game_api_proxy_when_opted_in(self):
        with mock.patch.dict(os.environ, {"PUNI_SOCKS_URL": "socks5://h:1", "PUNI_TOR_GAME_API": "1"}, clear=False):
            self.assertEqual(puni_service._game_api_proxy(), "socks5://h:1")

    def test_no_proxy_when_unset(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PUNI_SOCKS_URL", "PUNI_PROXY", "PUNI_TOR_GAME_API")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(puni_service._login_proxy())
            self.assertIsNone(puni_service._game_api_proxy())


class PrepareEngine(unittest.TestCase):
    """P-12/P-13：コピー判定。"""

    def test_needs_copy_when_missing(self):
        self.assertTrue(puni_service._needs_copy(__file__, "/nonexistent/xyz"))

    def test_no_copy_when_identical(self):
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, "a"); dst = os.path.join(d, "b")
            with open(src, "w") as f:
                f.write("x" * 100)
            shutil.copy2(src, dst)
            self.assertFalse(puni_service._needs_copy(src, dst))
        finally:
            import shutil as sh
            sh.rmtree(d, ignore_errors=True)


class AppIntegration(unittest.TestCase):
    """app.py との結合（画面・注文・ジョブ）。他部門への回帰も確認。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PUNI_DATA_DIR", "/tmp/puni_t")
        import app
        cls.app = app
        cls.client = app.app.test_client()

    def test_puni_page_renders(self):
        r = self.client.get("/puni")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("puni_coin_gacha", body)
        self.assertIn("使い捨て", body)   # P-04 注意

    def test_run_rejects_solo_combo(self):
        r = self.client.post("/run", json={"puni": {
            "mode": "email", "login": "a@b.com", "password": "x",
            "menus": ["puni_account", "puni_farm"],
            "qty": {"puni_account": "1", "puni_farm": "5"},
            "stages": {"puni_farm": "29704006"}}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("同時に選べません", r.get_json()["error"])

    def test_run_rejects_bad_email(self):
        r = self.client.post("/run", json={"puni": {
            "mode": "email", "login": "not-an-email", "password": "x",
            "menus": ["puni_hitodama"], "qty": {"puni_hitodama": "500"}}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("メールアドレス", r.get_json()["error"])

    def test_run_rejects_unlimited_out_of_range(self):
        r = self.client.post("/run", json={"puni": {
            "mode": "email", "login": "a@b.com", "password": "x",
            "menus": ["puni_unlimited"], "qty": {"puni_unlimited": "9"},
            "stages": {"puni_unlimited": "29704006"}}})
        self.assertEqual(r.status_code, 400)

    def _run_puni_job(self, menus, out, payload):
        with self.app.app.test_request_context():
            job = self.app._new_puni_job("email", menus)
        with mock.patch.object(self.app.puni_service, "run_order", return_value=out):
            self.app._run_puni_job(job, payload)
        return job

    def test_run_puni_job_marks_outcome_unknown_on_partial(self):
        """P-02：delivered な失敗は outcome_unknown=True（自動再実行させない）。"""
        out = {"ok": False, "error": "3/10 周で中断", "lines": ["■ 自動周回：一部完了（…）"],
               "done": 0, "account": None, "moved": False, "delivered": True}
        job = self._run_puni_job(["puni_farm"], out,
                                 {"mode": "email", "login": "a@b.com", "password": "x",
                                  "menus": ["puni_farm"], "qty": {"puni_farm": 10},
                                  "stages": {"puni_farm": 29704006}})
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["outcome_unknown"])

    def test_run_puni_job_retry_on_clean_failure(self):
        """何も提供していない失敗は outcome_unknown=False（払い直しなし再実行に回す）。"""
        out = {"ok": False, "error": "ログインできません", "lines": [], "done": 0,
               "account": None, "moved": False, "delivered": False}
        job = self._run_puni_job(["puni_hitodama"], out,
                                 {"mode": "email", "login": "a@b.com", "password": "x",
                                  "menus": ["puni_hitodama"], "qty": {"puni_hitodama": 500},
                                  "stages": {}})
        self.assertEqual(job["status"], "error")
        self.assertFalse(job["outcome_unknown"])

    def test_run_puni_job_done_on_success(self):
        out = {"ok": True, "error": "", "lines": ["■ コインガチャ：完了"], "done": 1,
               "account": None, "moved": False, "delivered": True}
        job = self._run_puni_job(["puni_coin_gacha"], out,
                                 {"mode": "email", "login": "a@b.com", "password": "x",
                                  "menus": ["puni_coin_gacha"], "qty": {}, "stages": {}})
        self.assertEqual(job["status"], "done")

    def test_status_total_is_mode_aware(self):
        """P-16：puni ジョブの進捗 total は PUNI_TOTAL_STEPS。"""
        self.assertEqual(self.app.PUNI_TOTAL_STEPS, 10)
        self.assertNotEqual(self.app.TOTAL_STEPS, self.app.PUNI_TOTAL_STEPS)
        # status API が使う対応表（P-16 の修正箇所と同じロジック）
        table = {"tsum": self.app.TSUM_TOTAL_STEPS, "puni": self.app.PUNI_TOTAL_STEPS}
        self.assertEqual(table.get("puni", self.app.TOTAL_STEPS), 10)
        self.assertEqual(table.get("nyanko", self.app.TOTAL_STEPS), self.app.TOTAL_STEPS)

    def test_lock_subject_distinguishes_departments(self):
        """_is_tsum_arguments と _lock_subject が puni/tsum/nyanko を取り違えない。"""
        puni_job = {"mode": "puni"}
        tsum_job = {"mode": "tsum"}
        self.assertTrue(self.app._lock_subject(puni_job, ({"login": "a@b.com"},)).startswith("puni:"))
        self.assertTrue(self.app._lock_subject(tsum_job, ({"login": "x"},)).startswith("tsum:"))

    def test_home_and_sitemap_have_puni(self):
        self.assertIn("/puni", self.client.get("/").get_data(as_text=True))
        self.assertIn("/puni", self.client.get("/sitemap.xml").get_data(as_text=True))

    def test_nyanko_and_tsum_pages_unaffected(self):
        self.assertEqual(self.client.get("/nyanko").status_code, 200)
        self.assertEqual(self.client.get("/tsum").status_code, 200)

    def test_forget_done_order_hides_it(self):
        app = self.app
        oid = "TESTFORGET1"
        with mock.patch.object(app, "_owner", return_value="owner-A"):
            with app._orders_lock:
                app._orders[oid] = {"owner": "owner-A", "uid": "x", "state": "done",
                                    "amount": 50, "created": __import__("time").time(),
                                    "job": {"mode": "puni"}, "result": None}
            try:
                r = self.client.post(f"/api/orders/{oid}/forget")
                self.assertEqual(r.status_code, 200)
                self.assertTrue(app._orders[oid]["hidden_by_owner"])
                mine = self.client.get("/api/orders/mine").get_json()["orders"]
                self.assertNotIn(oid, [o["order"] for o in mine])
            finally:
                with app._orders_lock:
                    app._orders.pop(oid, None)

    def test_forget_paid_order_is_blocked(self):
        app = self.app
        oid = "TESTFORGET2"
        with mock.patch.object(app, "_owner", return_value="owner-A"):
            with app._orders_lock:
                app._orders[oid] = {"owner": "owner-A", "uid": "x", "state": "paid",
                                    "amount": 50, "created": __import__("time").time(),
                                    "job": {"mode": "puni"}}
            try:
                r = self.client.post(f"/api/orders/{oid}/forget")
                self.assertEqual(r.status_code, 409)
                self.assertFalse(app._orders[oid].get("hidden_by_owner"))
            finally:
                with app._orders_lock:
                    app._orders.pop(oid, None)

    def test_forget_other_owner_is_404(self):
        app = self.app
        oid = "TESTFORGET3"
        with mock.patch.object(app, "_owner", return_value="owner-A"):
            with app._orders_lock:
                app._orders[oid] = {"owner": "someone-else", "uid": "y", "state": "done",
                                    "amount": 50, "created": __import__("time").time(),
                                    "job": {"mode": "puni"}}
            try:
                r = self.client.post(f"/api/orders/{oid}/forget")
                self.assertEqual(r.status_code, 404)
            finally:
                with app._orders_lock:
                    app._orders.pop(oid, None)

    def test_mode_new_with_action_menu_does_not_create_accounts(self):
        """なりすまし対策：mode=new + 非作成メニューでも、アカウント作成に化けない。
        経路はメニューから決まるので、周回メニューはログイン経路（資格情報が要る）になる。"""
        created = {"n": 0}

        def fake_create(*a, **k):
            created["n"] += 1
            return [{"email": "x@y.com", "password": "p", "player_name": "ぷに1"}], ""

        captured = {}

        def fake_login(c_mod, email, password, log):
            captured["email"] = email
            raise RuntimeError("login attempted")  # 資格情報が無いので失敗で良い

        with mock.patch.object(self.app.puni_service, "_create_accounts", side_effect=fake_create), \
             mock.patch.object(self.app.puni_service, "_login", side_effect=fake_login), \
             mock.patch.object(self.app.puni_service, "core", lambda: types.SimpleNamespace()):
            out = self.app.puni_service.run_order(
                "new", "attacker@x.com", "pw", ["puni_farm"],
                {"puni_farm": 5}, {"puni_farm": 29704006})
        self.assertEqual(created["n"], 0)          # アカウントは作られない
        self.assertFalse(out["ok"])
        self.assertIn("email", captured)           # ログイン経路に入った

    def test_run_order_routes_account_only_for_solo_menu(self):
        with mock.patch.object(self.app.puni_service, "_create_accounts",
                               return_value=([{"email": "a@b.com", "password": "p", "player_name": "ぷに1"}], "")) as cre, \
             mock.patch.object(self.app.puni_service, "_login") as login, \
             mock.patch.object(self.app.puni_service, "core", lambda: types.SimpleNamespace()):
            out = self.app.puni_service.run_order(
                "new", "", "", ["puni_account"], {"puni_account": 1}, {})
        self.assertTrue(out["ok"])
        cre.assert_called_once()
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AccountModeFollowsMenus(unittest.TestCase):
    """新規垢作成なのに「メールアドレスを入れて」と出ていた不具合（画面側）。入力方法は選んだメニューで決める。"""

    def test_page(self):
        import app as A
        html = A.app.test_client().get("/puni").get_data(as_text=True)
        self.assertIn("function mode(){ return isCreate()?'new':'email'; }", html)   # ラジオではなくメニューで決める
        self.assertIn('id="acctPickNote"', html)                                       # 「新しい垢を作る」だけ選んだときの案内
        self.assertIn("if(!isCreate()&&(!$('#login').value.trim()", html)             # メールを求めるのは、作成系でないときだけ
        self.assertNotIn("if(mode()!=='new'&&", html)
