"""ぷにぷに代行（puni_service / pricing.compute_puni_amount）の単体テスト。
tsum 側の test_tsum.py と同じ型で、画面・PayPay は触らずに検査だけ回す。"""
from __future__ import annotations

import unittest

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

    def test_unlimited_must_be_30min_step(self):
        _, _, _, err = puni_service.validate_order(
            ["puni_unlimited"], {"puni_unlimited": "45"},
            {"puni_unlimited": "29704006"})
        self.assertIn("30分単位", err)

    def test_stage_id_range(self):
        # puni_stage_clear は qty（数量＝ID）と stages（ステージID）の両方を持つので、
        # qty は通る値を入れてステージ側の範囲外だけを検査する。
        _, _, _, err = puni_service.validate_order(
            ["puni_stage_clear"], {"puni_stage_clear": "29704006"},
            {"puni_stage_clear": "999"})
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
        self.assertIn("入力", puni_service.check_account("email", "a@b.c", ""))
        self.assertIn("入力", puni_service.check_account("email", "", "x"))

    def test_email_needs_at(self):
        self.assertIn("メールアドレス", puni_service.check_account("email", "no-at-sign", "x"))

    def test_email_ok(self):
        self.assertEqual(puni_service.check_account("email", "a@b.c", "x"), "")

    def test_new_skips_credentials(self):
        self.assertEqual(puni_service.check_account("new", "", ""), "")

    def test_bad_mode(self):
        self.assertIn("入力方法", puni_service.check_account("xxx", "a@b.c", "x"))


class ComputePuniAmount(unittest.TestCase):
    def test_farm_rounds_up_per_10(self):
        amt, rows = pricing.compute_puni_amount(["puni_farm"], {"puni_farm": 25})
        # 25周 → 10周×3件分 → 50円×3 = 150
        self.assertEqual(amt, 3 * pricing.PRICES["puni_farm_10"])
        self.assertEqual(rows[0][1], 3)

    def test_unlimited_rounds_up_per_hour(self):
        amt, _ = pricing.compute_puni_amount(["puni_unlimited"], {"puni_unlimited": 120})
        self.assertEqual(amt, 2 * pricing.PRICES["puni_hour"])

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


if __name__ == "__main__":
    unittest.main()
