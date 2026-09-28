"""The built Web App served on the API's origin (PAW-060, Decision 0044)."""

import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.web import (
    ASSET_CACHE_CONTROL,
    INDEX_CACHE_CONTROL,
    WEB_CONTENT_SECURITY_POLICY,
)

from .support import FakeDatabase, make_client, make_settings

INDEX = b"<!doctype html><title>PAW</title><div id=root></div>"


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_bytes(INDEX)
        (self.dist / "assets" / "index-abc123.js").write_text("console.log(1)")
        (self.dist / "favicon.svg").write_text("<svg/>")
        (self.dist / ".hidden").write_text("secret")
        (root / "outside.txt").write_text("outside")
        os.symlink(root / "outside.txt", self.dist / "link.txt")
        app = create_app(
            make_settings(web_dist_dir=str(self.dist)), database=FakeDatabase()
        )
        self.client = make_client(app)

    def test_root_and_client_routes_are_the_index_page(self):
        for path in ("/", "/settings/devices", "/pair", "/login"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, INDEX)
                self.assertEqual(response.headers["cache-control"], INDEX_CACHE_CONTROL)
                self.assertEqual(
                    response.headers["content-security-policy"],
                    WEB_CONTENT_SECURITY_POLICY,
                )
                # The other defensive headers still apply.
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_hashed_assets_are_cached_and_other_files_revalidated(self):
        asset = self.client.get("/assets/index-abc123.js")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers["cache-control"], ASSET_CACHE_CONTROL)
        icon = self.client.get("/favicon.svg")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.headers["cache-control"], INDEX_CACHE_CONTROL)

    def test_head_is_answered_without_a_body(self):
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_api_paths_are_never_the_web_app(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["status"], "ok")
        for path in ("/api", "/api/v1/nope", "/api/nope"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "not_found")
                self.assertEqual(
                    response.headers["content-security-policy"],
                    "default-src 'none'; frame-ancestors 'none'",
                )

    def test_hidden_missing_and_escaping_files_are_not_served(self):
        for path in (
            "/.hidden",
            "/missing.js",
            "/assets/missing.js",
            "/link.txt",
            "/../outside.txt",
            "/assets/..%2F..%2Foutside.txt",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"secret", response.content)
                self.assertNotIn(b"outside", response.content)

    def test_a_name_the_file_system_refuses_is_not_an_error(self):
        # ENAMETOOLONG from the file system must not become a 500.
        response = self.client.get("/" + "a" * 5000 + ".js")
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/" + "a" * 5000)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, INDEX)

    def test_state_changing_methods_go_to_the_application(self):
        response = self.client.post("/settings")
        self.assertIn(response.status_code, (404, 405))
        self.assertNotEqual(response.content, INDEX)


class WebAppDisabledTest(unittest.TestCase):
    def test_without_a_build_directory_only_the_api_is_served(self):
        client = make_client(create_app(make_settings(), database=FakeDatabase()))
        self.assertEqual(client.get("/").status_code, 404)
        self.assertEqual(client.get("/pair").status_code, 404)


class WebDistSettingTest(unittest.TestCase):
    def test_the_directory_must_be_absolute_and_contain_index_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                make_settings(web_dist_dir=tmp)  # no index.html
            with self.assertRaises(ValidationError):
                make_settings(web_dist_dir="relative/dist")
            Path(tmp, "index.html").write_bytes(INDEX)
            settings = make_settings(web_dist_dir=tmp)
            self.assertEqual(settings.web_dist_dir, Path(tmp).resolve())

    def test_an_index_html_that_links_outside_the_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            (root / "secret.txt").write_text("secret")
            os.symlink(root / "secret.txt", dist / "index.html")
            with self.assertRaises(ValidationError):
                make_settings(web_dist_dir=str(dist))

    def test_an_index_html_swapped_for_an_outside_link_later_is_not_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_bytes(INDEX)
            (root / "secret.txt").write_text("secret")
            client = make_client(
                create_app(
                    make_settings(web_dist_dir=str(dist)), database=FakeDatabase()
                )
            )
            # Served while it is a file of the build (the middleware is built now).
            self.assertEqual(client.get("/").content, INDEX)
            (dist / "index.html").unlink()
            os.symlink(root / "secret.txt", dist / "index.html")
            for path in ("/", "/pair", "/index.html"):
                response = client.get(path)
                self.assertNotIn(b"secret", response.content, path)
                self.assertEqual(response.status_code, 404, path)

    def test_an_empty_value_means_unset(self):
        self.assertIsNone(make_settings(web_dist_dir="").web_dist_dir)


if __name__ == "__main__":
    unittest.main()
