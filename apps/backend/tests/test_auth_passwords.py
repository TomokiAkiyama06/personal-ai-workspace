"""Password policy and Argon2id hashing (PAW-022, no database)."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from argon2 import Type, extract_parameters

from paw_backend.auth import limits
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    PasswordPolicyError,
    PasswordProblem,
)
from paw_backend.auth.passwords import (
    COMMON_PASSWORDS,
    HashParameters,
    PasswordHasher,
    normalize_password,
    validate_new_password,
)

from .support import make_settings

FAST = HashParameters(time_cost=1, memory_kib=19_456, parallelism=1)


def problem_of(password, login_name=None) -> PasswordProblem:
    try:
        validate_new_password(password, login_name)
    except PasswordPolicyError as error:
        return error.problem
    raise AssertionError("the password was accepted")


class PolicyTest(unittest.TestCase):
    def test_the_minimum_length_is_ten_characters_as_the_requirements_say(self):
        self.assertEqual(limits.PASSWORD_MIN_LENGTH, 10)
        nine = "kq7w-zp3x"
        ten = "kq7w-zp3xr"
        self.assertEqual((len(nine), len(ten)), (9, 10))
        self.assertEqual(problem_of(nine), PasswordProblem.TOO_SHORT)
        self.assertEqual(validate_new_password(ten), ten)

    def test_length_counts_characters_not_bytes(self):
        # Ten Japanese characters are 30 bytes; nine are not enough.
        self.assertEqual(
            validate_new_password("ひらがなでつくるぱすわ"), "ひらがなでつくるぱすわ"
        )
        self.assertEqual(
            problem_of("ひらがなでつくるぱす"[:9]), PasswordProblem.TOO_SHORT
        )

    def test_no_composition_rules_a_long_lower_case_passphrase_is_fine(self):
        passphrase = "correct horse battery staple"
        self.assertEqual(validate_new_password(passphrase), passphrase)
        # ... and a short-looking mix of every class is not enough by itself.
        self.assertEqual(problem_of("Aa1!Aa1!"), PasswordProblem.TOO_SHORT)

    def test_the_upper_bound_is_in_characters_and_in_bytes(self):
        longest = ("abcdefghij" * 26)[: limits.PASSWORD_MAX_LENGTH]
        self.assertEqual(validate_new_password(longest), longest)
        self.assertEqual(problem_of(longest + "a"), PasswordProblem.TOO_LONG)
        # Distinct 3-byte characters: too many characters, whatever their bytes.
        distinct = "".join(
            chr(0x4E00 + i) for i in range(limits.PASSWORD_MAX_LENGTH + 1)
        )
        self.assertEqual(problem_of(distinct), PasswordProblem.TOO_LONG)
        # NFKC turns one U+FDFA into 18 characters: the length is judged AFTER it.
        self.assertEqual(problem_of("\ufdfa" * 15), PasswordProblem.TOO_LONG)

    def test_a_huge_input_is_refused_without_being_normalised(self):
        with patch("unicodedata.normalize", side_effect=AssertionError("normalised")):
            self.assertEqual(
                problem_of("a" * (limits.PASSWORD_MAX_UTF8_BYTES + 1)),
                PasswordProblem.TOO_LONG,
            )

    def test_characters_that_cannot_be_typed_or_stored_are_refused(self):
        for bad in (
            "valid-prefix\x00-suffix",
            "line1\nline2-abc",
            "tab\tbed-abcdef",
            "lone-surrogate-\ud800-x",
            "bell\x07-abcdefgh",
            "del-\x7f-abcdefgh",
        ):
            with self.subTest(bad=bad.encode("unicode_escape")):
                self.assertEqual(problem_of(bad), PasswordProblem.INVALID_CHARACTER)

    def test_the_common_passwords_are_refused_in_every_case(self):
        for common in (
            "password123",
            "PASSWORD123",
            "Qwertyuiop",
            "1234567890",
            "iloveyou123",
            "  ",
        ):
            with self.subTest(common=common):
                if len(common.strip()) < 10 and common.strip():
                    continue
                if not common.strip():
                    continue
                self.assertEqual(problem_of(common), PasswordProblem.TOO_COMMON)

    def test_every_listed_common_password_meets_the_length_rule(self):
        # The list is only useful for what would pass the length rule.
        for password in COMMON_PASSWORDS:
            with self.subTest(password=password):
                self.assertGreaterEqual(len(password), limits.PASSWORD_MIN_LENGTH)
                self.assertEqual(password, password.casefold())

    def test_a_repeated_pattern_is_refused(self):
        for pattern in ("aaaaaaaaaa", "1212121212", "abcabcabcabc", "zzzzzzzzzzzzzzz"):
            with self.subTest(pattern=pattern):
                self.assertEqual(problem_of(pattern), PasswordProblem.TOO_COMMON)
        # Four distinct characters is not a pattern the rule catches.
        self.assertEqual(validate_new_password("abcdabcdabcd"), "abcdabcdabcd")

    def test_the_login_name_is_refused_as_a_password_or_inside_one(self):
        self.assertEqual(
            problem_of("tomoki.akiyama", "tomoki.akiyama"),
            PasswordProblem.CONTAINS_LOGIN_NAME,
        )
        self.assertEqual(
            problem_of("My-TOMOKI.akiyama-42", "tomoki.akiyama"),
            PasswordProblem.CONTAINS_LOGIN_NAME,
        )
        # A short name inside a long passphrase is not a weakness (the rule only
        # looks for names of 6 characters or more); equality always is.
        self.assertEqual(limits.PASSWORD_LOGIN_NAME_CONTAINED_FROM, 6)
        passphrase = "tomatoes are red today"
        self.assertEqual(validate_new_password(passphrase, "tom"), passphrase)
        self.assertEqual(problem_of("abc", "abc"), PasswordProblem.TOO_SHORT)
        self.assertEqual(problem_of("tom" * 4, "tom"), PasswordProblem.TOO_COMMON)

    def test_a_login_name_of_the_wrong_type_is_refused_with_a_typed_error(self):
        with self.assertRaises(InvalidAuthInputError):
            validate_new_password("a-fine-passphrase", 42)

    def test_the_password_is_nfkc_normalised_before_it_is_checked_and_hashed(self):
        # Full-width letters and digits, and a composed / decomposed kana pair.
        fullwidth = "ｐａｓｓｗｏｒｄ１２３"
        self.assertEqual(normalize_password(fullwidth), "password123")
        self.assertEqual(problem_of(fullwidth), PasswordProblem.TOO_COMMON)
        composed = "\u304c\u304e\u3050\u3052\u3054" * 2
        decomposed = "\u304b\u3099\u304d\u3099\u304f\u3099\u3051\u3099\u3053\u3099" * 2
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(normalize_password(composed), normalize_password(decomposed))

    def test_a_value_that_is_not_a_string_is_a_typed_error_not_an_exception(self):
        for value in (None, 12345678901, b"bytes-password", ["a"], 1.5, True):
            with self.subTest(value=repr(value)):
                with self.assertRaises(InvalidAuthInputError):
                    validate_new_password(value)
                with self.assertRaises(InvalidAuthInputError):
                    normalize_password(value)

    def test_an_error_never_quotes_the_password(self):
        secret = "sh0rt-s3cret"[:9]
        try:
            validate_new_password(secret)
        except PasswordPolicyError as error:
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, repr(error))


class HasherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hasher = PasswordHasher(FAST, concurrency=2)
        self.addCleanup(self.hasher.close)

    async def test_it_makes_argon2id_with_the_configured_parameters(self):
        encoded = await self.hasher.hash("a passphrase that is long enough")
        self.assertTrue(encoded.startswith("$argon2id$v=19$m=19456,t=1,p=1$"))
        parameters = extract_parameters(encoded)
        self.assertEqual(parameters.type, Type.ID)
        self.assertEqual(
            (parameters.memory_cost, parameters.time_cost, parameters.parallelism),
            (19_456, 1, 1),
        )
        self.assertEqual(parameters.salt_len, limits.ARGON2_SALT_BYTES)
        self.assertEqual(parameters.hash_len, limits.ARGON2_HASH_BYTES)

    async def test_the_stored_value_holds_no_plaintext(self):
        password = "a passphrase that is long enough"
        encoded = await self.hasher.hash(password)
        self.assertNotIn(password, encoded)
        self.assertNotIn("passphrase", encoded)

    async def test_the_same_password_gets_a_different_salt_each_time(self):
        first = await self.hasher.hash("a passphrase that is long enough")
        second = await self.hasher.hash("a passphrase that is long enough")
        self.assertNotEqual(first, second)

    async def test_verify_accepts_the_right_password_and_only_that(self):
        encoded = await self.hasher.hash("a passphrase that is long enough")
        self.assertIs(
            await self.hasher.verify(encoded, "a passphrase that is long enough"), True
        )
        self.assertIs(
            await self.hasher.verify(encoded, "a passphrase that is long enougH"), False
        )
        self.assertIs(await self.hasher.verify(encoded, "x"), False)

    async def test_verify_uses_the_normalised_password(self):
        encoded = await self.hasher.hash("ｐａｓｓｗｏｒｄ-ｘｙｚ-12")
        self.assertIs(await self.hasher.verify(encoded, "password-xyz-12"), True)

    async def test_a_hash_that_is_not_argon2id_is_a_mismatch_never_an_error(self):
        bad = (
            "",
            "plain-text-password",
            "$argon2i$v=19$m=19456,t=1,p=1$c29tZXNhbHQ$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "$argon2d$v=19$m=19456,t=1,p=1$c29tZXNhbHQ$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "$argon2id$garbage",
            "$argon2id$v=19$m=99999999999,t=1,p=1$c29tZXNhbHQ$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            None,
            12,
        )
        for encoded in bad:
            with self.subTest(encoded=repr(encoded)[:40]):
                self.assertIs(
                    await self.hasher.verify(encoded, "some password here"), False
                )

    async def test_a_hash_that_asks_for_too_much_memory_is_not_verified(self):
        # 2 GiB: refused before Argon2 could allocate it.
        huge = (
            "$argon2id$v=19$m=2097152,t=1,p=1$c29tZXNhbHQ$"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        with patch.object(
            type(self.hasher._argon2), "verify", side_effect=AssertionError("ran")
        ):
            self.assertIs(await self.hasher.verify(huge, "some password here"), False)

    async def test_needs_rehash_is_true_for_other_parameters_and_for_garbage(self):
        current = await self.hasher.hash("a passphrase that is long enough")
        self.assertIs(self.hasher.needs_rehash(current), False)
        stronger = PasswordHasher(HashParameters(2, 19_456, 1), concurrency=1)
        self.addCleanup(stronger.close)
        self.assertIs(
            self.hasher.needs_rehash(
                await stronger.hash("a passphrase that is long enough")
            ),
            True,
        )
        self.assertIs(self.hasher.needs_rehash("not a hash"), True)

    async def test_verify_unknown_takes_about_the_time_of_a_real_verification(self):
        encoded = await self.hasher.hash("a passphrase that is long enough")
        calls = []
        real = self.hasher._argon2.verify

        def counting(encoded_hash, password):
            calls.append(extract_parameters(encoded_hash))
            return real(encoded_hash, password)

        with patch.object(type(self.hasher._argon2), "verify", side_effect=counting):
            await self.hasher.verify(encoded, "wrong password value")
            await self.hasher.verify_unknown("wrong password value")
        # Same work: one Argon2 verification each, with the same parameters.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    async def test_verify_unknown_never_reports_a_match(self):
        self.assertIsNone(await self.hasher.verify_unknown("whatever password 1"))

    async def test_a_password_that_cannot_be_one_is_a_typed_error_everywhere(self):
        for call in (
            lambda: self.hasher.hash(None),
            lambda: self.hasher.hash("nul\x00-inside-password"),
            lambda: self.hasher.verify("$argon2id$x", 5),
            lambda: self.hasher.verify_unknown("x" * 5000),
        ):
            with self.assertRaises((InvalidAuthInputError, PasswordPolicyError)):
                await call()

    async def test_hashing_does_not_block_the_event_loop(self):
        # A ticker keeps running while a (slower) hash is computed on its thread.
        slow = PasswordHasher(HashParameters(3, 65_536, 4), concurrency=1)
        self.addCleanup(slow.close)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            for _ in range(3):
                await slow.hash("a passphrase that is long enough")
        finally:
            task.cancel()
        self.assertGreater(ticks, 3)

    async def test_at_most_the_configured_number_of_hashes_run_at_once(self):
        hasher = PasswordHasher(FAST, concurrency=2)
        self.addCleanup(hasher.close)
        running = 0
        peak = 0
        lock = threading.Lock()
        release = threading.Event()

        def gated(password):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(5)
            with lock:
                running -= 1
            return "$argon2id$stub"

        with patch.object(type(hasher._argon2), "hash", side_effect=gated):
            tasks = [
                asyncio.create_task(hasher.hash(f"password-number-{i}-xx"))
                for i in range(6)
            ]
            for _ in range(2000):
                await asyncio.sleep(0.005)
                if peak >= 2:
                    break
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)

    async def test_too_many_waiting_jobs_are_refused_not_queued_without_limit(self):
        hasher = PasswordHasher(FAST, concurrency=1)
        self.addCleanup(hasher.close)
        release = threading.Event()

        def gated(password):
            release.wait(5)
            return "$argon2id$stub"

        with (
            patch.object(limits, "PASSWORD_HASH_MAX_PENDING", 2),
            patch("paw_backend.auth.passwords.PASSWORD_HASH_MAX_PENDING", 2),
            patch.object(type(hasher._argon2), "hash", side_effect=gated),
        ):
            accepted = [
                asyncio.create_task(hasher.hash(f"password-number-{i}-xx"))
                for i in range(3)
            ]
            await asyncio.sleep(0.05)
            with self.assertRaises(AuthUnavailableError):
                await hasher.hash("one more password value")
            release.set()
            await asyncio.gather(*accepted)
        # The count went back down: a new job is accepted again.
        self.assertTrue(
            (await hasher.hash("password after the burst")).startswith("$argon2id$")
        )

    async def test_a_closed_hasher_refuses_work_and_close_is_idempotent(self):
        hasher = PasswordHasher(FAST, concurrency=1)
        await hasher.hash("a passphrase that is long enough")
        hasher.close()
        hasher.close()
        with self.assertRaises(AuthUnavailableError):
            await hasher.hash("a passphrase that is long enough")

    async def test_a_caller_that_is_cancelled_frees_its_place(self):
        hasher = PasswordHasher(FAST, concurrency=1)
        self.addCleanup(hasher.close)
        release = threading.Event()
        started = threading.Event()

        def gated(password):
            started.set()
            release.wait(5)
            return "$argon2id$stub"

        with patch.object(type(hasher._argon2), "hash", side_effect=gated):
            first = asyncio.create_task(hasher.hash("password-number-1-xx"))
            for _ in range(2000):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            for _ in range(2000):
                if hasher._pending == 0:
                    break
                await asyncio.sleep(0.005)
        self.assertEqual(hasher._pending, 0)


class ConstructionTest(unittest.TestCase):
    def test_bad_arguments_are_refused(self):
        for parameters, concurrency, error in (
            ("params", 2, TypeError),
            (FAST, True, TypeError),
            (FAST, "2", TypeError),
            (FAST, 0, ValueError),
            (FAST, 17, ValueError),
        ):
            with self.subTest(concurrency=concurrency):
                with self.assertRaises(error):
                    PasswordHasher(parameters, concurrency=concurrency)

    def test_the_defaults_are_the_rfc_9106_low_memory_option(self):
        settings = make_settings()
        self.assertEqual(
            (
                settings.password_hash_time_cost,
                settings.password_hash_memory_kib,
                settings.password_hash_parallelism,
            ),
            (3, 65_536, 4),
        )
        hasher = PasswordHasher.from_settings(settings)
        self.addCleanup(hasher.close)
        self.assertEqual(hasher.parameters, HashParameters(3, 65_536, 4))


if __name__ == "__main__":
    unittest.main()
