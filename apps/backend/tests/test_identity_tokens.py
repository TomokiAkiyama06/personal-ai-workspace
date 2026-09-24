"""One-time token generation, parsing and verification (no database)."""

import base64
import hmac
import unittest
import uuid
from unittest.mock import patch

from paw_backend.identity import tokens


def secret_of(token: str) -> str:
    return token.split(".")[2]


class GenerationTest(unittest.TestCase):
    def test_a_token_has_the_documented_shape(self):
        new = tokens.generate()

        self.assertRegex(new.token, r"^pawst1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(new.token), tokens.TOKEN_LENGTH)
        self.assertEqual(tokens.TOKEN_LENGTH, 83)
        self.assertEqual(new.token.split(".")[1], new.token_id.hex)

    def test_the_secret_is_256_bits(self):
        secret = secret_of(tokens.generate().token)

        raw = base64.urlsafe_b64decode(secret + "=")
        self.assertEqual(len(raw), 32)

    def test_tokens_do_not_repeat(self):
        generated = [tokens.generate() for _ in range(500)]

        self.assertEqual(len({new.token for new in generated}), 500)
        self.assertEqual(len({new.token_id for new in generated}), 500)
        self.assertEqual(len({new.salt for new in generated}), 500)
        self.assertEqual(len({new.secret_hash for new in generated}), 500)

    def test_the_secret_comes_from_the_secrets_module(self):
        with patch.object(tokens.secrets, "token_urlsafe", return_value="A" * 43) as m:
            new = tokens.generate()

        m.assert_called_once_with(32)
        self.assertEqual(secret_of(new.token), "A" * 43)

    def test_only_a_salted_hash_is_kept(self):
        new = tokens.generate()
        secret = secret_of(new.token)

        self.assertEqual(len(new.salt), 16)
        self.assertEqual(len(new.secret_hash), 32)
        self.assertEqual(new.secret_hash, tokens.hash_secret(new.salt, secret))
        self.assertNotIn(secret.encode(), new.secret_hash + new.salt)

    def test_the_same_secret_hashes_differently_under_another_salt(self):
        secret = secret_of(tokens.generate().token)

        first = tokens.hash_secret(bytes(16), secret)
        second = tokens.hash_secret(bytes([1]) * 16, secret)

        self.assertNotEqual(first, second)

    def test_repr_hides_the_token_the_salt_and_the_hash(self):
        new = tokens.generate()

        text = repr(new) + repr(tokens.parse(new.token))

        self.assertNotIn(new.token, text)
        self.assertNotIn(secret_of(new.token), text)
        self.assertNotIn(repr(new.salt), text)
        self.assertNotIn(repr(new.secret_hash), text)


class ParsingTest(unittest.TestCase):
    def test_a_generated_token_parses_to_its_id_and_secret(self):
        new = tokens.generate()

        parsed = tokens.parse(new.token)

        self.assertEqual(parsed.token_id, new.token_id)
        self.assertEqual(parsed.secret, secret_of(new.token))

    def test_anything_else_is_not_a_token(self):
        good = tokens.generate().token
        token_id, secret = good.split(".")[1:]
        bad = {
            "empty": "",
            "wrong prefix": f"pawst2.{token_id}.{secret}",
            "no prefix": f"{token_id}.{secret}",
            "upper-case id": f"pawst1.{token_id.upper()}.{secret}",
            "short id": f"pawst1.{token_id[:-1]}.{secret}",
            "short secret": f"pawst1.{token_id}.{secret[:-1]}",
            "long secret": f"pawst1.{token_id}.{secret}A",
            "bad character": f"pawst1.{token_id}.{secret[:-1]}!",
            "padding": f"pawst1.{token_id}.{secret[:-1]}=",
            "trailing newline": good + "\n",
            "leading space": " " + good,
            "hyphenated id": f"pawst1.{uuid.UUID(hex=token_id)}.{secret}",
            "non-ascii": f"pawst1.{token_id}.{secret[:-1]}é",
            "huge": good * 1000,
            "not a string: None": None,
            "not a string: bytes": good.encode(),
            "not a string: int": 5,
            "not a string: uuid": uuid.uuid4(),
        }
        for name, value in bad.items():
            with self.subTest(name):
                self.assertIsNone(tokens.parse(value))


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.new = tokens.generate()
        self.parsed = tokens.parse(self.new.token)

    def test_the_right_secret_verifies(self):
        self.assertTrue(tokens.verify(self.parsed, self.new.salt, self.new.secret_hash))

    def test_a_wrong_secret_a_wrong_salt_or_a_wrong_hash_do_not(self):
        other = tokens.generate()
        wrong_secret = tokens.ParsedToken(self.new.token_id, secret_of(other.token))

        self.assertFalse(
            tokens.verify(wrong_secret, self.new.salt, self.new.secret_hash)
        )
        self.assertFalse(tokens.verify(self.parsed, other.salt, self.new.secret_hash))
        self.assertFalse(tokens.verify(self.parsed, self.new.salt, other.secret_hash))

    def test_nothing_verifies_without_a_stored_row_even_the_dummy_secret(self):
        dummy = tokens.ParsedToken(self.new.token_id, "A" * 43)

        self.assertFalse(tokens.verify(None, None, None))
        self.assertFalse(tokens.verify(self.parsed, None, None))
        self.assertFalse(tokens.verify(dummy, None, None))
        self.assertFalse(tokens.verify(None, self.new.salt, self.new.secret_hash))

    def test_the_comparison_is_constant_time_and_is_made_once_on_every_path(self):
        real_compare = hmac.compare_digest
        cases = {
            "match": (self.parsed, self.new.salt, self.new.secret_hash),
            "mismatch": (self.parsed, bytes(16), self.new.secret_hash),
            "unknown row": (self.parsed, None, None),
            "malformed": (None, None, None),
        }
        for name, arguments in cases.items():
            with self.subTest(name):
                with patch.object(
                    tokens.hmac, "compare_digest", side_effect=real_compare
                ) as compare:
                    tokens.verify(*arguments)
                self.assertEqual(compare.call_count, 1)
                a, b = compare.call_args.args
                self.assertEqual((len(a), len(b)), (32, 32))

    def test_the_hash_is_an_hmac_sha256_keyed_with_the_salt(self):
        expected = hmac.new(self.new.salt, secret_of(self.new.token).encode(), "sha256")

        self.assertEqual(self.new.secret_hash, expected.digest())


if __name__ == "__main__":
    unittest.main()
