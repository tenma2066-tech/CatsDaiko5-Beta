"""ミニゲーム（彩狩・/hunt/、チンチロ・/chinchiro/）の入口と、チンチロの画面の返し方を確かめる。"""
import unittest
from unittest.mock import patch

import app as A
import site_config


class MinigameMenu(unittest.TestCase):
    def setUp(self):
        self.cleanup = patch.object(A, "_cleanup_started", True)
        self.cleanup.start()
        self.addCleanup(self.cleanup.stop)
        self.client = A.app.test_client()

    def test_hunt_card_follows_setting(self):
        with patch.object(site_config, "HUNT_ENABLED", False):
            self.assertNotIn('href="/hunt/"', self.client.get("/").text)
        with patch.object(site_config, "HUNT_ENABLED", True):
            html = self.client.get("/").text
        self.assertIn('href="/hunt/"', html)
        self.assertIn("彩狩", html)

    def test_chinchiro_card_follows_setting(self):
        with patch.object(site_config, "CHINCHIRO_ENABLED", True):
            html = self.client.get("/").text
        self.assertIn('href="/chinchiro/"', html)
        self.assertIn("チンチロ", html)
        with patch.object(site_config, "CHINCHIRO_ENABLED", False):
            self.assertNotIn('href="/chinchiro/"', self.client.get("/").text)

    def test_all_minigames_in_one_menu(self):
        with patch.object(site_config, "DYA_ENABLED", True), patch.object(site_config, "OSERO_ENABLED", True), \
             patch.object(site_config, "HUNT_ENABLED", True), patch.object(site_config, "CHINCHIRO_ENABLED", True):
            html = self.client.get("/").text
        nav = html[html.index('href="/dya/"'):]
        nav = nav[:nav.index("</nav>")]
        for path in ("/osero/", "/hunt/", "/chinchiro/"):
            self.assertIn(f'href="{path}"', nav)

    def test_chinchiro_page(self):
        r = self.client.get("/chinchiro/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("チンチロ", r.get_data(as_text=True))
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'none'", csp)            # 外へは通信しない
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'unsafe-inline'", csp)     # 1ファイルの中のスクリプトは動かす
        self.assertNotIn("nonce-", csp)                      # お店の画面の方針で上書きされていない
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get("/chinchiro").status_code, 308)

    def test_chinchiro_off(self):
        with patch.object(site_config, "CHINCHIRO_ENABLED", False):
            self.assertEqual(self.client.get("/chinchiro/").status_code, 404)

    def test_not_indexed(self):
        robots = self.client.get("/robots.txt").text
        self.assertIn("Disallow: /hunt/", robots)
        self.assertIn("Disallow: /chinchiro/", robots)
        sitemap = self.client.get("/sitemap.xml").text
        self.assertNotIn("/hunt", sitemap)
        self.assertNotIn("/chinchiro", sitemap)


if __name__ == "__main__":
    unittest.main()
