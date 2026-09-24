import unittest

from paw_backend.identity import InvalidLoginNameError, normalize_login_name


class NormaliseTest(unittest.TestCase):
    def test_names_are_lower_cased_and_trimmed(self):
        for raw, expected in {
            "Tomoki": "tomoki",
            "  owner.one  ": "owner.one",
            "A_B-C.9": "a_b-c.9",
            "abc": "abc",
            "0" * 64: "0" * 64,
        }.items():
            with self.subTest(raw):
                self.assertEqual(normalize_login_name(raw), expected)

    def test_unicode_compatibility_forms_are_folded_to_ascii(self):
        # Full-width letters are the same name as their ASCII form.
        self.assertEqual(normalize_login_name("ＡＤＭＩＮ１"), "admin1")

    def test_normalisation_is_idempotent(self):
        once = normalize_login_name("  Some.Name_1 ")

        self.assertEqual(normalize_login_name(once), once)

    def test_length_boundaries(self):
        self.assertEqual(normalize_login_name("a" * 3), "aaa")
        self.assertEqual(normalize_login_name("a" * 64), "a" * 64)
        for length in (0, 1, 2, 65, 300):
            with self.subTest(length):
                with self.assertRaises(InvalidLoginNameError):
                    normalize_login_name("a" * length)

    def test_bad_names_are_rejected(self):
        for raw in [
            "has space",
            "at@sign",
            ".leading-dot",
            "trailing-",
            "_underscore-first",
            "two\nlines",
            "nul\x00byte",
            "tab\tinside",
            "日本語の名前",
            "café",
            "a/b",
            "'; drop table users; --",
            "",
            "   ",
        ]:
            with self.subTest(raw):
                with self.assertRaises(InvalidLoginNameError):
                    normalize_login_name(raw)

    def test_only_strings_are_accepted(self):
        for value in (None, 5, b"owner", ["owner"], object()):
            with self.subTest(value):
                with self.assertRaises(InvalidLoginNameError):
                    normalize_login_name(value)

    def test_the_error_never_quotes_the_input(self):
        with self.assertRaises(InvalidLoginNameError) as caught:
            normalize_login_name("hunter2 with spaces")

        self.assertNotIn("hunter2", str(caught.exception))
        self.assertNotIn("hunter2", repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
