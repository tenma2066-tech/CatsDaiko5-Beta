"""全体の読み直しで見つけた不具合の再発防止テスト（PayPay・ゲームへの通信はモック）。"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as A
import paypay_gateway as G
import puni_service
import site_config
import tsum_service


class Base(unittest.TestCase):
    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True),
            patch.object(G, "history_upsert"), patch.object(G, "mark_used"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def paid_retry_order(self, body):
        """支払い済みだが実行に失敗した注文（払い直しなしで再実行できる状態）を作る。"""
        j = self.c.post("/run", json=body).get_json()
        A._orders[j["order"]].update(state="paid", retry=True)
        return j


class CreditAndCoupons(Base):
    def test_cheap_paid_order_cannot_pay_for_bigger_order_via_free_coupon(self):
        cheap = self.paid_retry_order({"items": ["XP"]})                         # 25円を支払い済み（実行は失敗）
        free = G.coupon_issue_free(30)
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J1")):
            r = self.c.post("/run", json={"chars": "1-9", "items": ["XP"], "coupon": free}).get_json()
        self.assertNotEqual(r.get("order"), cheap["order"])                      # 安い支払いを充てない
        self.assertFalse(r.get("prepaid"))
        self.assertEqual(G.coupon_list()[0]["state"], "used")                    # 無料クーポンはちゃんと使われる
        self.assertEqual(A._orders[cheap["order"]]["state"], "paid")             # 25円の注文はそのまま残る

    def test_same_value_order_is_still_credited_and_coupon_is_kept(self):
        first = self.paid_retry_order({"chars": "1-5"})                          # 250円を支払い済み
        code = G.coupon_issue("OLD", 30)
        r = self.c.post("/run", json={"chars": "2-6", "coupon": code}).get_json()
        self.assertEqual((r["order"], r["prepaid"]), (first["order"], True))     # 同じ金額 → 払い直しなし
        self.assertFalse(any(b.get("coupon") for b in r["breakdown"]))           # クーポンの行は出さない
        self.assertEqual(G.coupon_check(code), (True, ""))                       # クーポンは使われずに残る

    def test_reserved_coupon_is_released_when_a_held_order_expires(self):
        code = G.coupon_issue("OLD", 30)
        j = self.c.post("/run", json={"chars": "1-5", "coupon": code}).get_json()
        self.assertFalse(G.coupon_check(code)[0])                                # 注文が押さえている
        A._orders[j["order"]].update(state="hold", link_code="abcDEF123456", created=time.time() - 8 * 86400)
        A._gc_orders()                                                           # 7日で消える
        self.assertNotIn(j["order"], A._orders)
        self.assertEqual(G.coupon_check(code), (True, ""))                       # クーポンは使える状態に戻る


class Listing(Base):
    def test_orders_being_rechecked_stay_in_the_list(self):
        j = self.c.post("/run", json={"items": ["XP"]}).get_json()
        for state in ("paying", "starting", "hold", "paid", "dead"):
            A._orders[j["order"]]["state"] = state
            rows = self.c.get("/api/orders/mine").get_json()["orders"]
            self.assertEqual([r["state"] for r in rows], [state])

    def test_absurd_numbers_are_rejected_not_500(self):
        for body in ({"plan": "period", "days": 1e999}, {"plan": "period", "days": "9" * 5000}):
            self.assertEqual(self.c.post("/api/ticket/order", json=body).status_code, 400)
        self.assertEqual(self.c.post("/api/support/order", json={"amount": 1e999}).status_code, 400)
        huge = "9" * 5000
        self.assertTrue(tsum_service.validate_order(["tsum_coin"], {"tsum_coin": huge})[2])
        self.assertTrue(puni_service.validate_order(["puni_farm"], {"puni_farm": huge}, {"puni_farm": "1001001"})[3])

    def test_products_need_payments_enabled(self):
        with patch.object(A, "PAYMENTS_ENABLED", False):
            r = self.c.post("/api/ticket/order", json={"plan": "once"})
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "payments_off"))


class PayPayRelogin(unittest.TestCase):
    """セッション切れのあと、確認のたびにパスワードでログインし直さない（SMS の連発を防ぐ）。"""

    def setUp(self):
        self.account = {"phone": "08012345678", "password": "pw", "uuid": "u", "expires_at": 0, "session_cookie": ""}
        self.patches = [patch.object(G, "_load_account", return_value=dict(self.account)),
                        patch.object(G, "_save_account"),
                        patch.dict(G._relogin_block, {"until": 0.0, "code": "", "message": ""}),
                        patch.dict(G._pending_otp, {}, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_otp_prompt_is_not_repeated(self):
        otp = AsyncMock(return_value={"kind": "otp", "otp_reference_id": "ref", "otp_prefix": "AB", "cookies": {}})
        with patch.object(G, "_oauth", otp):
            for _ in range(5):
                with self.assertRaises(G.PayPayError) as ctx:
                    asyncio.run(G._fresh_account())
                self.assertEqual(ctx.exception.code, "otp_required")
            self.assertEqual(otp.await_count, 1)                                  # ログインは1回だけ
            self.assertEqual(G._pending_otp["otp_reference_id"], "ref")           # 管理者が入力中のコードは有効なまま
            with self.assertRaises(G.PayPayError):                                # 管理者の「接続を更新」は今すぐ試す
                asyncio.run(G._fresh_account(force=True, manual=True))
            self.assertEqual(otp.await_count, 2)

    def test_wrong_password_backs_off_then_recovers(self):
        bad = AsyncMock(side_effect=G.PayPayError("oauth_rejected", "x"))
        with patch.object(G, "_oauth", bad):
            for _ in range(3):
                with self.assertRaises(G.PayPayError):
                    asyncio.run(G._fresh_account())
        self.assertEqual(bad.await_count, 1)
        G._relogin_block["until"] = time.time() - 1                               # 休み時間が過ぎた
        good = AsyncMock(return_value={"kind": "token", "access_token": "t" * 12, "expires_in": 3600,
                                       "session_cookie": "c", "cookies": {}})
        with patch.object(G, "_oauth", good):
            self.assertEqual(asyncio.run(G._fresh_account())["session_cookie"], "c")
        self.assertEqual(G._relogin_block["until"], 0.0)


class Services(unittest.TestCase):
    def test_tsum_takeover_still_returns_account_when_menus_crash(self):
        fake_core = type("C", (), {"DEBUG_HOOK": None})()
        with patch.object(tsum_service, "core", return_value=fake_core), \
             patch.object(tsum_service, "_login_guest", return_value=("sess", "forge", "")), \
             patch.object(tsum_service, "_session_user", return_value="1"), \
             patch.object(tsum_service, "_run_menus", side_effect=RuntimeError("boom")), \
             patch.object(tsum_service, "_issue_transfer", return_value=("NEWID", "NEWPW")) as issue:
            out = tsum_service.run_order("guest", "OLDID", "pw", ["tsum_coin_max"], {})
        issue.assert_called_once()                                                # 例外でも引き継ぎ情報は発行する
        self.assertEqual((out["transfer"], out["confirm"], out["ok"]), ("NEWID", "NEWPW", False))
        self.assertIn("RuntimeError", out["error"])
        self.assertNotIn("boom", out["error"])

    def test_puni_account_without_save_is_not_reported_as_success(self):
        rec = {"email": "a@example.com", "password": "pw", "player_name": "n", "client": None, "userId": None}
        with patch.object(puni_service, "core", return_value=object(), create=True), \
             patch.object(puni_service, "_create_level5_id", return_value=rec):
            accounts, error = puni_service._create_accounts(object(), "puni_account", 1,
                                                            puni_service._Progress(None), lambda _t: None)
        self.assertEqual(error, "")
        self.assertIs(accounts[0]["save"], False)

    def test_puni_error_text_hides_urls_and_tokens(self):
        msg = puni_service._safe_message(RuntimeError(
            "GET https://api.example.com/v1?token=abc failed for key AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
        self.assertNotIn("example.com", msg)
        self.assertNotIn("AAAAAAAA", msg)
        self.assertIn("failed", msg)


class ShopIdentity(unittest.TestCase):
    def test_every_page_uses_site_config(self):
        c = A.app.test_client()
        with patch.object(site_config, "SHOP_NAME", "テスト商店"), patch.object(site_config, "SHOP_X_ID", "test_shop"), \
             patch.object(site_config, "REVIEW_POST_ID", ""), patch.object(site_config, "GOOGLE_SITE_VERIFICATION", ""):
            for path in ("/", "/nyanko", "/tsum", "/puni", "/ticket", "/support", "/source"):
                page = c.get(path).get_data(as_text=True)
                self.assertIn("テスト商店", page, path)
                self.assertNotIn("TN SHOP", page, path)
                self.assertNotIn("paysms1212", page, path)
            self.assertNotIn("google-site-verification", c.get("/").get_data(as_text=True))
            self.assertIn("const REVIEW_POST=\"\"", c.get("/nyanko").get_data(as_text=True))   # レビューのお願いは出ない

    def test_empty_env_value_falls_back_to_default(self):
        with patch.dict("os.environ", {"X_TEST_VALUE": "  "}):
            self.assertEqual(site_config._env("X_TEST_VALUE", "既定"), "既定")
        with patch.dict("os.environ", {"X_TEST_VALUE": " 値 "}):
            self.assertEqual(site_config._env("X_TEST_VALUE", "既定"), "値")

    def test_home_shows_real_lowest_prices(self):
        with patch.dict(A.PRICES, {k: 300 for k in A.PRICES if k.startswith("tsum_")}), \
             patch.dict(A.PRICES, {"tsum_medal": 120}):
            self.assertEqual(A._lowest_prices()["tsum"], 120)


if __name__ == "__main__":
    unittest.main()
