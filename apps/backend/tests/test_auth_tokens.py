"""Session ids and the hashed keys of the throttles and the audit trail."""

import hashlib
import unittest
import uuid

from paw_backend.auth import tokens
from paw_backend.auth.limits import SESSION_TOKEN_BYTES, SESSION_TOKEN_LENGTH


class SessionTokenTest(unittest.TestCase):
    def test_a_token_is_256_random_bits_in_43_url_safe_characters(self):
        token = tokens.new_session_token()
        self.assertEqual(len(token), SESSION_TOKEN_LENGTH)
        self.assertEqual(SESSION_TOKEN_BYTES, 32)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(tokens.parse_session_token(token), token)

    def test_tokens_do_not_repeat(self):
        self.assertEqual(len({tokens.new_session_token() for _ in range(2000)}), 2000)

    def test_only_the_exact_shape_of_a_token_is_parsed(self):
        good = "A" * 43
        for bad in (
            None,
            42,
            b"A" * 43,
            "",
            "A" * 42,
            "A" * 44,
            "A" * 42 + "=",
            "A" * 42 + " ",
            "A" * 42 + "\n",
            "A" * 42 + "/",
            "é" * 43,
            "A" * 21 + "\x00" + "A" * 21,
        ):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertIsNone(tokens.parse_session_token(bad))
        self.assertEqual(tokens.parse_session_token(good), good)

    def test_the_stored_hash_is_sha256_with_a_label_and_is_not_the_token(self):
        token = "B" * 43
        stored = tokens.hash_session_token(token)
        self.assertEqual(len(stored), 32)
        self.assertEqual(
            stored, hashlib.sha256(b"paw.session.v1\0" + b"B" * 43).digest()
        )
        self.assertNotEqual(stored, token.encode())
        self.assertNotIn(token.encode(), stored)
        self.assertEqual(stored, tokens.hash_session_token(token))
        self.assertNotEqual(stored, tokens.hash_session_token("C" * 43))


class KeysTest(unittest.TestCase):
    def test_keys_of_different_kinds_never_collide(self):
        values = {
            tokens.account_key("alice"),
            tokens.source_key("alice"),
            tokens.hash_session_token("alice".ljust(43, "x")),
            tokens.GLOBAL_KEY,
        }
        self.assertEqual(len(values), 4)
        self.assertTrue(all(len(value) == 32 for value in values))

    def test_a_key_is_stable(self):
        self.assertEqual(tokens.account_key("alice"), tokens.account_key("alice"))
        self.assertNotEqual(tokens.account_key("alice"), tokens.account_key("alicf"))

    def test_a_name_that_is_not_a_login_name_is_normalised_like_one(self):
        self.assertEqual(tokens.raw_account_name("  ＡＬＩＣＥ  "), "alice")
        self.assertEqual(tokens.raw_account_name("Zoë!"), "zoë!")
        self.assertEqual(len(tokens.raw_account_name("x" * 5000)), 128)


class SourceBucketTest(unittest.TestCase):
    def test_an_ipv4_address_is_its_own_bucket(self):
        self.assertEqual(tokens.source_bucket("203.0.113.7"), "203.0.113.7")

    def test_an_ipv6_address_is_bucketed_by_its_64_bit_prefix(self):
        first = tokens.source_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
        second = tokens.source_bucket("2001:db8:1:2::1")
        other = tokens.source_bucket("2001:db8:1:3::1")
        self.assertEqual(first, "2001:db8:1:2::/64")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_an_ipv4_mapped_ipv6_address_is_the_ipv4_bucket(self):
        self.assertEqual(tokens.source_bucket("::ffff:203.0.113.7"), "203.0.113.7")

    def test_a_scope_id_is_ignored(self):
        self.assertEqual(tokens.source_bucket("fe80::1%eth0"), "fe80::/64")

    def test_anything_else_is_one_unknown_bucket(self):
        for value in (
            None,
            "",
            "testclient",
            "not an address",
            "999.1.1.1",
            5,
            b"1.2.3.4",
        ):
            with self.subTest(value=repr(value)):
                self.assertEqual(tokens.source_bucket(value), "unknown")

    def test_the_audit_id_is_a_stable_pseudonym_that_is_not_the_address(self):
        one = tokens.source_audit_id("203.0.113.7")
        self.assertIsInstance(one, uuid.UUID)
        self.assertEqual(one, tokens.source_audit_id("203.0.113.7"))
        self.assertNotEqual(one, tokens.source_audit_id("203.0.113.8"))
        self.assertNotEqual(one.bytes[:4], bytes([203, 0, 113, 7]))


if __name__ == "__main__":
    unittest.main()
