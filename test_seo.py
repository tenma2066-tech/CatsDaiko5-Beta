"""公開 URL と検索対象が、ホスト名や注文 API に混ざらないことを確認する。"""
import os
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import app as A


class SearchIndexing(unittest.TestCase):
    def setUp(self):
        self.origin = "https://nyanko-daiko-tnshop.duckdns.org"
        self.env = patch.dict(os.environ, {"SITE_URL": self.origin + "/"})
        self.cleanup = patch.object(A, "_cleanup_started", True)
        self.env.start()
        self.cleanup.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.cleanup.stop)
        self.client = A.app.test_client()

    def test_sitemap_lists_only_public_pages(self):
        response = self.client.get("/sitemap.xml", base_url="https://alternate.example")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        root = ElementTree.fromstring(response.data)
        locations = [node.text for node in root.findall("{*}url/{*}loc")]
        self.assertEqual(locations, [self.origin + p for p in ("/", "/nyanko", "/tsum")])

    def test_robots_points_to_canonical_sitemap(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertIn("Sitemap: " + self.origin + "/sitemap.xml", response.text)
        for path in ("/api/", "/status/", "/admin", "/health"):
            self.assertIn("Disallow: " + path, response.text)

    def test_public_pages_have_canonical_without_query(self):
        for path in ("/", "/nyanko", "/tsum"):
            with self.subTest(path=path):
                response = self.client.get(path + "?ref=share", base_url="https://alternate.example")
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'<link rel="canonical" href="{self.origin}{path}">', response.text)
                self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", ""))

    def test_internal_and_missing_pages_are_not_indexed(self):
        for path in ("/admin", "/api/state", "/health", "/status/missing", "/not-found"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.headers.get("X-Robots-Tag"), "noindex, nofollow")

    def test_unconfigured_site_uses_https_proxy_origin(self):
        with patch.dict(os.environ, {"SITE_URL": ""}):
            with A.app.test_request_context("/", headers={"X-Forwarded-Proto": "https"}):
                self.assertEqual(A._site(), "https://localhost")

    def test_site_url_rejects_paths_and_credentials(self):
        for value in ("https://example.com/path", "https://user:pass@example.com", "javascript:bad"):
            with patch.dict(os.environ, {"SITE_URL": value}), A.app.test_request_context("/"):
                with self.assertRaises(ValueError):
                    A._site()


if __name__ == "__main__":
    unittest.main()
