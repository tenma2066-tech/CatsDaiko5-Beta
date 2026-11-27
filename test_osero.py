"""ミニゲーム（UltraOSERO・/osero/）の入口が、設定どおりに出たり隠れたりすることを確かめる。"""
import unittest
from unittest.mock import patch

import app as A
import site_config


class OseroMinigameEntry(unittest.TestCase):
    def setUp(self):
        self.cleanup = patch.object(A, "_cleanup_started", True)
        self.cleanup.start()
        self.addCleanup(self.cleanup.stop)
        self.client = A.app.test_client()

    def test_card_hidden_when_disabled(self):
        with patch.object(site_config, "OSERO_ENABLED", False):
            html = self.client.get("/").text
        self.assertNotIn('href="/osero/"', html)

    def test_card_shown_when_enabled(self):
        with patch.object(site_config, "OSERO_ENABLED", True):
            html = self.client.get("/").text
        self.assertIn('href="/osero/"', html)
        self.assertIn("UltraOSERO", html)

    def test_minigame_not_indexed(self):
        self.assertIn("Disallow: /osero/", self.client.get("/robots.txt").text)

    def test_minigame_not_in_sitemap(self):
        self.assertNotIn("/osero", self.client.get("/sitemap.xml").text)


if __name__ == "__main__":
    unittest.main()
