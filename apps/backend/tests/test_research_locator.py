"""``canonicalize_locator``: one canonical, sanitised spelling per source URL."""

import time
import traceback
import unittest

from paw_backend.research.providers import (
    MAX_LOCATOR_CHARS,
    InvalidLocatorError,
    canonicalize_locator,
)
from paw_backend.research.providers.locator import (
    CREDENTIAL_PARAMETERS,
    DEFAULT_PORTS,
    TRACKING_PARAMETER_PREFIXES,
    TRACKING_PARAMETERS,
)

from .research_support import SECRET


class ConstantsTest(unittest.TestCase):
    def test_tracking_parameters_are_pinned(self):
        self.assertEqual(TRACKING_PARAMETER_PREFIXES, ("utm_",))
        self.assertEqual(
            TRACKING_PARAMETERS,
            frozenset(
                {
                    "fbclid",
                    "gclid",
                    "dclid",
                    "gbraid",
                    "wbraid",
                    "msclkid",
                    "yclid",
                    "igshid",
                    "mc_cid",
                    "mc_eid",
                    "_ga",
                    "_gl",
                }
            ),
        )
        self.assertEqual(
            CREDENTIAL_PARAMETERS,
            frozenset(
                {
                    "access_token",
                    "id_token",
                    "refresh_token",
                    "token",
                    "api_key",
                    "apikey",
                    "auth",
                    "authorization",
                    "password",
                    "passwd",
                    "secret",
                    "client_secret",
                    "sig",
                    "signature",
                    "x-amz-signature",
                    "x-amz-credential",
                    "x-amz-security-token",
                }
            ),
        )
        self.assertEqual(dict(DEFAULT_PORTS), {"http": 80, "https": 443})


class CanonicalFormTest(unittest.TestCase):
    def check(self, cases: dict[str, str]) -> None:
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = canonicalize_locator(raw)
                self.assertIsInstance(result, str)
                self.assertEqual(result, expected)

    def test_already_canonical_urls_are_unchanged(self):
        same = [
            "https://example.com/",
            "https://example.com/a/b",
            "https://example.com/a/b/",
            "http://example.com/a?b=1",
            "https://example.com:8443/a",
            "https://docs.python.org/3/library/asyncio.html",
            "http://127.0.0.1:8080/x",
            "https://example.com/@user?email=a@b.c",
        ]
        self.check({url: url for url in same})

    def test_scheme_and_host_are_lower_cased_path_case_is_kept(self):
        self.check(
            {
                "HTTPS://Example.COM/Path/To": "https://example.com/Path/To",
                "Http://EXAMPLE.com/A?Q=B": "http://example.com/A?Q=B",
            }
        )

    def test_an_empty_path_becomes_a_slash(self):
        self.check(
            {
                "https://example.com": "https://example.com/",
                "https://example.com?x=1": "https://example.com/?x=1",
                "https://example.com#frag": "https://example.com/",
            }
        )

    def test_default_ports_are_dropped_other_ports_are_kept(self):
        self.check(
            {
                "http://example.com:80/x": "http://example.com/x",
                "https://example.com:443/x": "https://example.com/x",
                "http://example.com:0080/": "http://example.com/",
                "http://example.com:443/x": "http://example.com:443/x",
                "https://example.com:80/x": "https://example.com:80/x",
                "http://example.com:8080/x": "http://example.com:8080/x",
                "http://example.com:08080/x": "http://example.com:8080/x",
                "http://example.com:65535/": "http://example.com:65535/",
                "http://example.com:1/": "http://example.com:1/",
                "http://example.com:/x": "http://example.com/x",
            }
        )

    def test_one_trailing_dot_of_the_host_is_removed(self):
        self.check({"https://example.com./a": "https://example.com/a"})

    def test_the_fragment_is_removed(self):
        self.check(
            {
                "https://example.com/a#section": "https://example.com/a",
                "https://example.com/a?x=1#frag?y=2": "https://example.com/a?x=1",
                "https://example.com/#": "https://example.com/",
                "https://example.com/a/#top": "https://example.com/a/",
            }
        )

    def test_tracking_parameters_are_removed(self):
        self.check(
            {
                "https://example.com/a?utm_source=x&id=5&utm_medium=y": (
                    "https://example.com/a?id=5"
                ),
                "https://example.com/?UTM_Campaign=1&fbclid=abc&gclid=1&q=z": (
                    "https://example.com/?q=z"
                ),
                "https://example.com/a?utm_source=x": "https://example.com/a",
                "https://example.com/?utm_=1&k=v": "https://example.com/?k=v",
                "https://example.com/?_ga=1&a=2&_gl=3": "https://example.com/?a=2",
                (
                    "https://example.com/?msclkid=1&mc_cid=2&mc_eid=3&igshid=4"
                    "&yclid=5&dclid=6&gbraid=7&wbraid=8&k=v"
                ): "https://example.com/?k=v",
                "https://example.com/?FBCLID=1&Gclid=2&k=v": "https://example.com/?k=v",
            }
        )

    def test_credential_parameters_are_removed(self):
        self.check(
            {
                "https://example.com/a?access_token=S3&x=1": "https://example.com/a?x=1",
                "https://example.com/a?Token=S3": "https://example.com/a",
                "https://example.com/?API_KEY=S3&apikey=S4&q=z": (
                    "https://example.com/?q=z"
                ),
                (
                    "https://example.com/f?X-Amz-Signature=S3&X-Amz-Credential=S4"
                    "&X-Amz-Security-Token=S5&sig=S6&signature=S7&n=1"
                ): "https://example.com/f?n=1",
                "https://example.com/?password=p&passwd=p&secret=s&client_secret=c": (
                    "https://example.com/"
                ),
                (
                    "https://example.com/?auth=a&authorization=b&id_token=c"
                    "&refresh_token=d"
                ): "https://example.com/",
            }
        )

    def test_similar_names_are_not_credential_parameters(self):
        self.check(
            {
                "https://example.com/?key=k": "https://example.com/?key=k",
                "https://example.com/?tokens=1": "https://example.com/?tokens=1",
                "https://example.com/?my_token=1": "https://example.com/?my_token=1",
                "https://example.com/?author=me": "https://example.com/?author=me",
                "https://example.com/?q=token": "https://example.com/?q=token",
            }
        )

    def test_other_parameters_are_not_tracking(self):
        self.check(
            {
                "https://github.com/o/r?ref=main": "https://github.com/o/r?ref=main",
                "https://example.com/?utm=1": "https://example.com/?utm=1",
                "https://example.com/?xutm_source=1": "https://example.com/?xutm_source=1",
                "https://example.com/?fbclid2=1": "https://example.com/?fbclid2=1",
                "https://example.com/?source=utm_source": (
                    "https://example.com/?source=utm_source"
                ),
            }
        )

    def test_query_pieces_are_sorted_by_name_then_value(self):
        self.check(
            {
                "https://example.com/?b=2&a=1": "https://example.com/?a=1&b=2",
                "https://example.com/?a=2&a=1": "https://example.com/?a=1&a=2",
                "https://example.com/?b&a=1": "https://example.com/?a=1&b",
                "https://example.com/?B=1&a=2": "https://example.com/?B=1&a=2",
                "https://example.com/?a=b=c&a=a": "https://example.com/?a=a&a=b=c",
                "https://example.com/?c=3&b=2&a=1&d=4": (
                    "https://example.com/?a=1&b=2&c=3&d=4"
                ),
            }
        )

    def test_empty_query_pieces_are_dropped_and_pieces_keep_their_spelling(self):
        self.check(
            {
                "https://example.com/?a=1&&b=2&": "https://example.com/?a=1&b=2",
                "https://example.com/?": "https://example.com/",
                "https://example.com/?&&": "https://example.com/",
                "https://example.com/a?": "https://example.com/a",
                "https://example.com/?a=": "https://example.com/?a=",
                "https://example.com/?flag": "https://example.com/?flag",
                "https://example.com/?q=a+b": "https://example.com/?q=a+b",
                "https://example.com/?a=1;b=2": "https://example.com/?a=1;b=2",
            }
        )

    def test_percent_escapes_are_upper_cased(self):
        self.check(
            {
                "https://example.com/a%e6%97%a5?q=%c3%a9": (
                    "https://example.com/a%E6%97%A5?q=%C3%A9"
                ),
                "https://example.com/%2f%2F": "https://example.com/%2F%2F",
                "https://example.com/%zz%2": "https://example.com/%zz%2",
                "https://example.com/100%": "https://example.com/100%",
            }
        )

    def test_non_ascii_characters_are_percent_encoded_as_utf8(self):
        self.check(
            {
                "https://ja.wikipedia.org/wiki/日本語": (
                    "https://ja.wikipedia.org/wiki/%E6%97%A5%E6%9C%AC%E8%AA%9E"
                ),
                "https://example.com/?q=é": "https://example.com/?q=%C3%A9",
                "https://example.com/😀": "https://example.com/%F0%9F%98%80",
                "https://example.com/a%e6日": "https://example.com/a%E6%E6%97%A5",
            }
        )

    def test_a_percent_sign_does_not_hide_what_follows_it(self):
        self.check(
            {
                "https://example.com/%日本": "https://example.com/%%E6%97%A5%E6%9C%AC",
                "https://example.com/?q=%é": "https://example.com/?q=%%C3%A9",
                "https://example.com/%%e6": "https://example.com/%%E6",
                "https://example.com/%e%e6": "https://example.com/%e%E6",
                "https://example.com/?q=%%e6": "https://example.com/?q=%%E6",
                "https://example.com/%٤١": "https://example.com/%%D9%A4%D9%A1",
                "https://example.com/a%": "https://example.com/a%",
                "https://example.com/%4": "https://example.com/%4",
            }
        )

    def test_an_equals_sign_survives_escaping_of_the_name(self):
        self.check(
            {
                "https://example.com/?é=": "https://example.com/?%C3%A9=",
                "https://example.com/?é": "https://example.com/?%C3%A9",
                "https://example.com/?%e6=": "https://example.com/?%E6=",
                "https://example.com/?%e6": "https://example.com/?%E6",
                "https://example.com/?é=1&é=": "https://example.com/?%C3%A9=&%C3%A9=1",
            }
        )

    def test_escaping_happens_before_the_query_is_sorted(self):
        self.check(
            {"https://example.com/?é=1&a=2": "https://example.com/?%C3%A9=1&a=2"}
        )

    def test_path_is_otherwise_preserved(self):
        self.check(
            {
                "https://example.com/a/./b/../c": "https://example.com/a/./b/../c",
                "https://example.com//a//b": "https://example.com//a//b",
                "https://example.com/A": "https://example.com/A",
                "https://www.example.com/a": "https://www.example.com/a",
            }
        )

    def test_http_and_https_stay_different(self):
        self.assertNotEqual(
            canonicalize_locator("http://example.com/a"),
            canonicalize_locator("https://example.com/a"),
        )

    def test_hosts_of_the_allowed_shape_are_accepted(self):
        self.check(
            {
                "https://my-host1.example.co.jp/": "https://my-host1.example.co.jp/",
                "https://a.b/": "https://a.b/",
                "https://localhost/": "https://localhost/",
                "http://192.168.0.1/": "http://192.168.0.1/",
                "https://xn--r8jz45g.jp/": "https://xn--r8jz45g.jp/",
            }
        )

    def test_host_length_boundaries(self):
        label63 = "a" * 63
        host253 = ".".join([label63, label63, label63, "a" * 61])
        self.assertEqual(len(host253), 253)
        self.assertEqual(
            canonicalize_locator(f"https://{host253}/"), f"https://{host253}/"
        )
        self.assertEqual(
            canonicalize_locator(f"https://{label63}.example/"),
            f"https://{label63}.example/",
        )
        for host in (
            f"{label63}a.example",
            ".".join([label63, label63, label63, "a" * 62]),
        ):
            with (
                self.subTest(host_length=len(host)),
                self.assertRaises(InvalidLocatorError),
            ):
                canonicalize_locator(f"https://{host}/")

    def test_length_boundaries(self):
        prefix = "https://a.example/"
        exact = prefix + "x" * (MAX_LOCATOR_CHARS - len(prefix))
        self.assertEqual(len(exact), MAX_LOCATOR_CHARS)
        self.assertEqual(canonicalize_locator(exact), exact)
        with self.assertRaises(InvalidLocatorError):
            canonicalize_locator(exact + "x")

    def test_escaping_that_makes_the_result_too_long_is_rejected(self):
        raw = "https://a.example/" + "日" * 300
        self.assertLess(len(raw), MAX_LOCATOR_CHARS)
        with self.assertRaises(InvalidLocatorError):
            canonicalize_locator(raw)

    def test_the_result_is_idempotent(self):
        samples = [
            "HTTPS://Example.COM:443/A%e6/b?utm_source=x&b=2&a=1#frag",
            "http://example.com:08080/日本?q=é&&z",
            "https://example.com",
            "https://example.com./a/?",
            "https://example.com/?flag&a=&a=1",
        ]
        for raw in samples:
            with self.subTest(raw=raw):
                once = canonicalize_locator(raw)
                self.assertEqual(canonicalize_locator(once), once)

    def test_different_spellings_give_one_locator(self):
        spellings = [
            "https://Example.com/docs?b=2&a=1",
            "HTTPS://EXAMPLE.COM:443/docs?a=1&b=2#intro",
            "https://example.com./docs?utm_source=news&a=1&b=2&fbclid=z",
        ]
        self.assertEqual(
            {canonicalize_locator(raw) for raw in spellings},
            {"https://example.com/docs?a=1&b=2"},
        )


class RejectedLocatorTest(unittest.TestCase):
    def reject(self, values) -> None:
        for raw in values:
            with self.subTest(raw=raw), self.assertRaises(InvalidLocatorError):
                canonicalize_locator(raw)

    def test_empty_and_whitespace(self):
        self.reject(
            [
                "",
                " ",
                " https://example.com/",
                "https://example.com/ ",
                "https://exa mple.com/",
                "https://example.com/a b",
                "https://example.com/\n",
                "https://example.com/\t",
                "https://example.com/\r\n",
                "\u3000https://example.com/",
                "https://example.com/\u00a0",
                "https://example.com/\x00",
                "https://example.com/\x7f",
                "https://example.com/\ud800",
            ]
        )

    def test_schemes_other_than_http_and_https(self):
        self.reject(
            [
                "ftp://example.com/",
                "file:///etc/passwd",
                "javascript:alert(1)",
                "data:text/html,x",
                "mailto:someone@example.com",
                "ssh://git@github.com/o/r.git",
                "hxxp://example.com/",
                "example.com/x",
                "//example.com/x",
                "/relative/path",
                "?q=1",
                "https:example.com",
                "https:/example.com",
                "http:",
            ]
        )

    def test_missing_host(self):
        self.reject(
            ["https://", "https:///path", "https://:443/", "https://./", "http://?q=1"]
        )

    def test_user_information(self):
        self.reject(
            [
                "https://user@example.com/",
                "https://user:pass@example.com/",
                "https://@example.com/",
                "https://:@example.com/",
                "https://user:@example.com/",
                "https://example.com:80@evil.example/",
                "https://a@b@example.com/",
            ]
        )

    def test_backslashes(self):
        self.reject(
            [
                "https://example.com\\@evil.example/",
                "https://evil.example\\.example.com/",
                "https://example.com/a\\b",
            ]
        )

    def test_bad_ports(self):
        self.reject(
            [
                "https://example.com:abc/",
                "https://example.com:65536/",
                "https://example.com:0/",
                "https://example.com:-1/",
                "https://example.com:443:80/",
                "https://example.com:4 3/",
            ]
        )

    def test_hosts_outside_the_allowed_shape(self):
        self.reject(
            [
                "https://exa_mple.com/",
                "https://例え.jp/",
                "https://[::1]/",
                "https://[2001:db8::1]:8080/",
                "https://[/",
                "https://example..com/",
                "https://.example.com/",
                "https://-a.example.com/",
                "https://a-.example.com/",
                "https://ex%61mple.com/",
                "https://exa!mple.com/",
                "https://example.com../",
            ]
        )

    def test_non_ascii_hosts_that_fold_to_ascii_are_rejected(self):
        # ``str.lower`` turns KELVIN SIGN into "k" and would smuggle a
        # non-ASCII host through as an ASCII one.
        self.reject(
            [
                "https://exam\u212ale.com/",
                "https://\u212aey.example/",
                "https://\uff45xample.com/",
                "https://ex\u0130mple.com/",
            ]
        )

    def test_too_long_input(self):
        self.reject(["https://a.example/" + "x" * MAX_LOCATOR_CHARS])

    def test_huge_input_is_rejected_without_being_scanned(self):
        # The work must not grow with the size of the input.
        for raw in (
            "https://a.example/" + "x" * 20_000_000,
            "https://a.example/?" + "a=1&" * 5_000_000,
            "https://a.example/" + "日" * 5_000_000,
        ):
            with self.subTest(size=len(raw)):
                started = time.monotonic()
                with self.assertRaises(InvalidLocatorError):
                    canonicalize_locator(raw)
                self.assertLess(time.monotonic() - started, 2.0)

    def test_pathological_inputs_within_the_limit_are_fast(self):
        prefix = "https://a.example/"
        room = MAX_LOCATOR_CHARS - len(prefix)
        for raw in (
            prefix + "%" * room,
            prefix + "%zz" * (room // 3),
            prefix + "?" + "&" * (room - 1),
            prefix + "?" + "a=1&" * (room // 4),
            "https://" + "a" * 250 + ".example/",
            "https://" + "a." * 120 + "example/" + "x" * 1500,
        ):
            with self.subTest(raw=raw[:30]):
                started = time.monotonic()
                try:
                    canonicalize_locator(raw)
                except InvalidLocatorError:
                    pass
                self.assertLess(time.monotonic() - started, 2.0)

    def test_the_traceback_never_quotes_the_input(self):
        # ``urlsplit`` and ``SplitResult.port`` put the input in their messages;
        # a chained context would leak it into any logged traceback.
        for raw in (
            f"https://ex\u2100{SECRET}.com/x",
            f"https://example.com:443:{SECRET}/",
            f"https://[{SECRET}/",
            f"https://user:{SECRET}@example.com/",
        ):
            with self.subTest(raw=raw[:24]):
                with self.assertRaises(InvalidLocatorError) as caught:
                    canonicalize_locator(raw)
                text = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn(SECRET, text)

    def test_non_strings_are_a_type_error(self):
        for raw in (None, 5, b"https://example.com/", ["https://example.com/"]):
            with self.subTest(raw=raw), self.assertRaises(TypeError):
                canonicalize_locator(raw)

    def test_the_error_never_echoes_the_input(self):
        raw = f"https://user:{SECRET}@example.com/{SECRET}"
        with self.assertRaises(InvalidLocatorError) as caught:
            canonicalize_locator(raw)
        self.assertEqual(str(caught.exception), "Invalid source locator")
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_credentials_never_survive_into_a_result(self):
        raw = f"https://example.com/a?access_token={SECRET}&x=1#{SECRET}"
        result = canonicalize_locator(raw)
        self.assertEqual(result, "https://example.com/a?x=1")
        self.assertNotIn(SECRET, result)


if __name__ == "__main__":
    unittest.main()
