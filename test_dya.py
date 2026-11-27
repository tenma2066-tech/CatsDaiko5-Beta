"""ミニゲーム（ダイナマイト野球・/dya/）の入口が、設定どおりに出たり隠れたりすることを確かめる。"""
import unittest
from unittest.mock import patch

import app as A
import site_config


class DyaMinigameEntry(unittest.TestCase):
    def setUp(self):
        self.cleanup = patch.object(A, "_cleanup_started", True)
        self.cleanup.start()
        self.addCleanup(self.cleanup.stop)
        self.client = A.app.test_client()

    def test_card_hidden_when_disabled(self):
        with patch.object(site_config, "DYA_ENABLED", False):
            html = self.client.get("/").text
        self.assertNotIn('href="/dya/"', html)

    def test_card_shown_when_enabled(self):
        with patch.object(site_config, "DYA_ENABLED", True):
            html = self.client.get("/").text
        self.assertIn('href="/dya/"', html)
        self.assertIn("ダイナマイト野球", html)

    def test_minigame_not_indexed(self):
        self.assertIn("Disallow: /dya/", self.client.get("/robots.txt").text)

    def test_minigame_not_in_sitemap(self):
        self.assertNotIn("/dya", self.client.get("/sitemap.xml").text)


if __name__ == "__main__":
    unittest.main()
