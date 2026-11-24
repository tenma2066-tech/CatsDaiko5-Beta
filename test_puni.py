"""ぷにぷに代行（puni_service / pricing.compute_puni_amount）の単体テスト。
tsum 側の test_tsum.py と同じ型で、画面・PayPay は触らずに検査だけ回す。"""
from __future__ import annotations

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

        def fake_action(c_mod, cli, menu, qty, stages, prog, log):
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


if __name__ == "__main__":
    unittest.main()
