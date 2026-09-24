import unittest

from paw_backend.security import host_from_header, normalize_origin, origin_allowed


class HostFromHeaderTest(unittest.TestCase):
    def test_strips_the_port_and_lowercases(self):
        for header, host in (
            ("localhost", "localhost"),
            ("LocalHost:8000", "localhost"),
            ("127.0.0.1:443", "127.0.0.1"),
            ("[::1]", "[::1]"),
            ("[::1]:8000", "[::1]"),
            (" workspace.example.org ", "workspace.example.org"),
        ):
            with self.subTest(header=header):
                self.assertEqual(host_from_header(header), host)

    def test_missing_or_malformed_headers_have_no_host(self):
        for header in (None, "", ":8000", "[::1"):
            with self.subTest(header=header):
                self.assertIsNone(host_from_header(header))


class NormalizeOriginTest(unittest.TestCase):
    def test_canonical_form_drops_default_ports_and_case(self):
        for value, origin in (
            ("https://App.Example.org", "https://app.example.org"),
            ("https://app.example.org:443", "https://app.example.org"),
            ("http://localhost:80/", "http://localhost"),
            ("http://localhost:8000", "http://localhost:8000"),
            ("http://[::1]:8000", "http://[::1]:8000"),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_origin(value), origin)

    def test_values_that_are_not_origins_are_rejected(self):
        for value in (
            "null",
            "",
            "app.example.org",
            "ftp://app.example.org",
            "https://app.example.org/path",
            "https://app.example.org?x=1",
            "https://user:pw@app.example.org",
            "https://app.example.org:notaport",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_origin(value))


class OriginAllowedTest(unittest.TestCase):
    def test_same_origin_means_the_authority_matches_the_host_header(self):
        self.assertTrue(origin_allowed("http://localhost:8000", "localhost:8000", []))
        self.assertTrue(
            origin_allowed("https://paw.example.org", "paw.example.org", [])
        )
        self.assertTrue(
            origin_allowed("https://paw.example.org", "paw.example.org:443", [])
        )
        self.assertFalse(origin_allowed("http://localhost:8000", "localhost:9000", []))
        self.assertFalse(origin_allowed("https://paw.example.org", None, []))

    def test_listed_origins_are_allowed_regardless_of_the_host(self):
        listed = ["https://app.example.org"]
        self.assertTrue(
            origin_allowed("https://APP.example.org:443", "localhost", listed)
        )
        self.assertFalse(origin_allowed("http://app.example.org", "localhost", listed))

    def test_malformed_origins_are_never_allowed(self):
        self.assertFalse(origin_allowed("null", "null", ["null"]))


if __name__ == "__main__":
    unittest.main()
