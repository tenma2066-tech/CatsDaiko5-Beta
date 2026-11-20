"""Web 管理画面を HTTP 経由で検証。外部の決済・Discord へは接続しない。"""
import re
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store
import web as owner_web


class OwnerWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        for name in ("CONFIG_FILE", "ORDERS_FILE", "ACCOUNTS_FILE", "USED_FILE", "HOLDS_FILE", "DETECT_FILE"):
            self.enterContext(patch.object(store, name, str(Path(tmp, Path(getattr(store, name)).name))))
        self.enterContext(patch.object(owner_web, "connections", return_value={"paypay": False, "kyash": False}))
        self.auth = Path(tmp, "password.hash")
        self.password = secrets.token_urlsafe()
        salt = secrets.token_bytes(16)
        self.auth.write_text(f"{salt.hex()}:{owner_web.hash_password(self.password.encode(), salt)}")
        self.client = TestClient(TestServer(owner_web.make_app(self.auth)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Host": "test.example", "Origin": "https://test.example"}

    async def post(self, url, **data):
        return await self.client.post(url, data=data, headers=self.headers, allow_redirects=False)

    async def login(self):
        response = await self.post("/login", password=self.password)
        self.assertEqual(response.status, 303)
        cookie = response.cookies[owner_web.COOKIE]
        self.assertTrue(cookie["httponly"] and cookie["secure"])
        self.headers["Cookie"] = f"{owner_web.COOKIE}={cookie.value}"
        response = await self.client.get("/", headers=self.headers)
        return re.search('name="csrf" value="([^"]+)"', await response.text()).group(1)

    async def test_private_pages_and_closed_setup(self):
        for url in ("/", "/connections", "/history"):
            response = await self.client.get(url, allow_redirects=False)
            self.assertEqual(response.headers["Location"], "/login")
        self.auth.unlink()
        self.assertEqual((await self.post("/login", password=self.password)).status, 503)

    async def test_quote_history_connections_and_logout(self):
        token = await self.login()
        response = await self.post("/do/estimate", direction="p2k", amount="3000", csrf=token)
        self.assertEqual(response.status, 303)
        for url, label in (("/", "¥2,850"), ("/history", "取引はまだありません"), ("/connections", "初回ログインは未対応")):
            self.assertIn(label, await (await self.client.get(url, headers=self.headers)).text())
        self.assertEqual((await self.post("/logout", csrf=token)).status, 303)
        self.assertEqual((await self.client.get("/", headers=self.headers, allow_redirects=False)).status, 303)

    async def test_auth_and_csrf(self):
        self.assertNotIn(owner_web.COOKIE, (await self.post("/login", password="wrong")).cookies)
        token = await self.login()
        self.assertEqual((await self.post("/do/settings", csrf="bad", enabled="yes")).status, 403)
        self.headers["Origin"] = "https://elsewhere.example"
        self.assertEqual((await self.post("/do/settings", csrf=token)).status, 403)

    async def test_exchange_requires_provider_and_binding(self):
        token = await self.login()
        args = dict(direction="p2k", link="https://pay.paypay.ne.jp/example", confirm="yes", csrf=token)
        with patch.object(owner_web.exchange, "process_p2k", new_callable=AsyncMock) as process:
            await self.post("/do/exchange", **args)
            process.assert_not_called()
            with patch.object(owner_web, "connections", return_value={"paypay": True, "kyash": True}):
                await self.post("/do/exchange", **args)
                process.assert_not_called()
                with patch.object(owner_web.aml, "gate", return_value={"state": "ok"}):
                    await self.post("/do/exchange", **args)
                    process.assert_awaited_once()
                    self.assertEqual(process.await_args.args[0].user.id, owner_web.OWNER)

    async def test_bound_registration_and_uncertain_no_retry(self):
        token = await self.login()
        with patch.object(owner_web, "connections", return_value={"paypay": True, "kyash": False}):
            await self.post("/do/challenge", csrf=token, service="paypay")
            self.assertIn("本人確認用送金リンク", await (await self.client.get("/connections", headers=self.headers)).text())
        order, _ = store.get_or_create_order("p2k", owner_web.OWNER, "管理者", 1000, 950, 50, 5,
                                              receipt_key="test", status="UNCERTAIN")
        with patch.object(owner_web.exchange, "payout", new_callable=AsyncMock) as send:
            await self.post("/do/retry", csrf=token, order=order["id"], confirm="yes")
            send.assert_not_called()
            await self.post("/do/confirm", csrf=token, order=order["id"], confirm="yes", result="sent")
            self.assertEqual(store.get_order(order["id"])["status"], "SENT")
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
