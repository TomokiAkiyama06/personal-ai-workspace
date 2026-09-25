"""The validators of the repository module (PAW-027): one table per argument.

Each row is a value and what the validator must do with it. Nothing here needs a
database or a file system.
"""

import unittest
import uuid

from paw_backend.authz.capabilities import RepoPermission
from paw_backend.repositories import InputProblem, InvalidRepositoryInputError, limits
from paw_backend.repositories import validation as v

INVALID = InputProblem.INVALID_FORMAT


class ValidatorTestCase(unittest.TestCase):
    def assertRefused(self, function, value, problem, *extra):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            function(value, *extra)
        self.assertEqual(raised.exception.problem, problem, repr(value)[:40])
        # The value is never echoed.
        self.assertNotIn(str(value)[:12] or "\0", str(raised.exception))


class NameTest(ValidatorTestCase):
    def test_good_names_are_returned_unchanged(self):
        for name in (
            "a",
            "repo",
            "Repo-2.x_y",
            "0day",
            "a" * limits.MAX_NAME_CHARS,
            "x.gitx",
            "tool.github",
        ):
            with self.subTest(name=name):
                self.assertEqual(v.validate_name(name), name)

    def test_bad_names_are_refused_with_the_reason(self):
        cases = [
            (None, InputProblem.NOT_A_STRING),
            (5, InputProblem.NOT_A_STRING),
            (b"repo", InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("a" * (limits.MAX_NAME_CHARS + 1), InputProblem.TOO_LONG),
            (
                "a" * (limits.MAX_NAME_CHARS * limits.RAW_TEXT_FACTOR + 1),
                InputProblem.TOO_LONG,
            ),
            (" repo", INVALID),
            ("repo ", INVALID),
            ("re po", INVALID),
            (".hidden", INVALID),
            ("-flag", INVALID),
            ("_under", INVALID),
            ("..", INVALID),
            (".", INVALID),
            ("a/b", INVALID),
            ("a\\b", INVALID),
            ("a:b", INVALID),
            ("tool.git", INVALID),
            ("TOOL.GIT", INVALID),
            ("日本語", INVALID),
            ("réo", INVALID),
            ("re\x00po", InputProblem.INVALID_CHARACTERS),
            ("re\npo", InputProblem.INVALID_CHARACTERS),
            ("re‮po", InputProblem.INVALID_CHARACTERS),
            ("re\ud800po", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                self.assertRefused(v.validate_name, value, problem)

    def test_the_field_name_is_reported(self):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            v.validate_name("", "other")
        self.assertEqual(raised.exception.field, "other")


class BranchTest(ValidatorTestCase):
    def test_good_branches(self):
        for branch in (
            "main",
            "feature/x",
            "release-1.2",
            "a/b/c",
            "v1.0.0",
            "a" * limits.MAX_BRANCH_CHARS,
        ):
            with self.subTest(branch=branch):
                self.assertEqual(v.validate_branch(branch), branch)

    def test_bad_branches(self):
        cases = [
            (None, InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("a" * (limits.MAX_BRANCH_CHARS + 1), InputProblem.TOO_LONG),
            ("-D", INVALID),
            ("--upload-pack=x", INVALID),
            ("/main", INVALID),
            ("main/", INVALID),
            ("a//b", INVALID),
            ("a/../b", INVALID),
            ("a..b", INVALID),
            (".hidden", INVALID),
            ("a/.hidden", INVALID),
            ("a.lock", INVALID),
            ("a/b.lock/c", INVALID),
            ("main.", INVALID),
            ("a b", INVALID),
            ("a~b", INVALID),
            ("a^b", INVALID),
            ("a:b", INVALID),
            ("a?b", INVALID),
            ("a*b", INVALID),
            ("a[b", INVALID),
            ("a@{b", INVALID),
            ("a\\b", INVALID),
            ("feature/日本語", INVALID),
            ("a\x00b", InputProblem.INVALID_CHARACTERS),
            ("a\tb", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                self.assertRefused(v.validate_branch, value, problem)


class PathTextTest(ValidatorTestCase):
    def test_a_canonical_absolute_path_is_returned_unchanged(self):
        for path in ("/home/alice/src/tool", "/a", "/a/b.c/d-e_f"):
            with self.subTest(path=path):
                self.assertEqual(v.validate_path_text(path), path)

    def test_the_encoded_length_is_bounded_as_well_as_the_characters(self):
        four = "\U00020000"  # 4 bytes in UTF-8
        at_limit = "/" + four * 511 + "h" * (limits.MAX_PATH_BYTES - 1 - 4 * 511)
        self.assertEqual(len(at_limit.encode()), limits.MAX_PATH_BYTES)
        self.assertEqual(v.validate_path_text(at_limit), at_limit)
        self.assertRefused(v.validate_path_text, at_limit + "h", InputProblem.TOO_LONG)
        # 1024 characters (the character limit) of 4 bytes: over the byte limit.
        long_text = "/" + four * (limits.MAX_PATH_CHARS - 1)
        self.assertRefused(v.validate_path_text, long_text, InputProblem.TOO_LONG)
        # Two-byte characters: 1024 of them are exactly 2048 bytes, and that is stored.
        two = "é" * 512
        self.assertEqual(v.validate_path_text("/" + two), "/" + two)

    def test_everything_else_is_refused(self):
        cases = [
            (None, InputProblem.NOT_A_STRING),
            (b"/a", InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("/", INVALID),
            ("relative/path", INVALID),
            ("./a", INVALID),
            ("~/a", INVALID),
            ("~alice/a", INVALID),
            ("/a/../b", INVALID),
            ("/a/..", INVALID),
            ("/a/.../b", INVALID),
            ("/a/./b", INVALID),
            ("/a//b", INVALID),
            ("/a/b/", INVALID),
            ("/a\\b", INVALID),
            ("/a/%2e%2e/b", INVALID),
            ("/a/%2Fb", INVALID),
            ("/a/%5cb", INVALID),
            ("/a/b\x00", InputProblem.INVALID_CHARACTERS),
            ("/a/\nb", InputProblem.INVALID_CHARACTERS),
            ("/a/‮b", InputProblem.INVALID_CHARACTERS),
            ("/a/ｂ", INVALID),  # full-width letter: not in NFKC form
            ("/" + "a" * limits.MAX_PATH_CHARS, InputProblem.TOO_LONG),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                self.assertRefused(v.validate_path_text, value, problem)


class RemoteUrlTest(ValidatorTestCase):
    def test_a_canonical_https_url_is_stored_in_the_form_the_broker_compares(self):
        cases = {
            "https://github.com/acme/tool": "https://github.com/acme/tool",
            "https://GitHub.com/acme/tool.git": "https://github.com/acme/tool.git",
            "HTTPS://github.com/acme/tool/": "https://github.com/acme/tool",
            "https://api.github.com/repos/acme/tool": (
                "https://api.github.com/repos/acme/tool"
            ),
            # A dotted-quad host is a valid remote for the URL-to-repository
            # mapping; which hosts may be *cloned* from is the policy's business.
            "https://10.1.2.3/acme/tool": "https://10.1.2.3/acme/tool",
        }
        for given, stored in cases.items():
            with self.subTest(url=given):
                self.assertEqual(v.validate_remote_url(given), stored)

    def test_everything_else_is_refused(self):
        cases = [
            (None, InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("http://github.com/acme/tool", INVALID),
            ("ssh://git@github.com/acme/tool", INVALID),
            ("git@github.com:acme/tool.git", INVALID),
            ("file:///srv/tool", INVALID),
            ("/srv/tool", INVALID),
            ("https://github.com", INVALID),
            ("https://github.com/", INVALID),
            ("https://user@github.com/acme/tool", INVALID),
            ("https://user:pw@github.com/acme/tool", INVALID),
            ("https://github.com:8443/acme/tool", INVALID),
            ("https://github.com/acme/tool?x=1", INVALID),
            ("https://github.com/acme/tool#frag", INVALID),
            ("https://github.com/acme/../tool", INVALID),
            ("https://github.com/acme/%2e%2e/tool", INVALID),
            ("https://github.com/acme\\tool", INVALID),
            ("https://github.com/acme/to ol", INVALID),
            ("https://github.com/acme/to@ol", INVALID),
            ("https://gıthub.com/acme/tool", INVALID),
            (
                "https://github.com/" + "a" * limits.MAX_REMOTE_URL_CHARS,
                InputProblem.TOO_LONG,
            ),
            ("https://github.com/\x00", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    v.validate_remote_url(value)
                if problem is not INVALID:
                    self.assertEqual(raised.exception.problem, problem)

    def test_what_the_database_would_refuse_is_refused_here_first(self):
        self.assertTrue(v.is_storable_remote("https://github.com/acme/tool"))
        for url in (
            "https://github.com/",
            "https://github.com/a b",
            "https://github.com/a@b",
            "http://github.com/a",
            "https://GitHub.com/a",
            "https://github.com/" + "a" * 1100,
        ):
            with self.subTest(url=url[:40]):
                self.assertFalse(v.is_storable_remote(url))


class PermissionsTest(ValidatorTestCase):
    def test_none_is_inherit_and_a_collection_of_members_is_a_set(self):
        self.assertIsNone(v.validate_permissions(None))
        self.assertEqual(v.validate_permissions([]), frozenset())
        self.assertEqual(
            v.validate_permissions((RepoPermission.READ, RepoPermission.READ)),
            frozenset({RepoPermission.READ}),
        )
        self.assertEqual(
            v.validate_permissions(iter(RepoPermission)), frozenset(RepoPermission)
        )

    def test_strings_and_foreign_members_are_refused(self):
        cases = [
            ("read", InputProblem.NOT_A_COLLECTION),
            (b"read", InputProblem.NOT_A_COLLECTION),
            (5, InputProblem.NOT_A_COLLECTION),
            (object(), InputProblem.NOT_A_COLLECTION),
            (["read"], InputProblem.NOT_A_PERMISSION),
            ([RepoPermission.READ, "write"], InputProblem.NOT_A_PERMISSION),
            ([None], InputProblem.NOT_A_PERMISSION),
            ([1], InputProblem.NOT_A_PERMISSION),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    v.validate_permissions(value)
                self.assertEqual(raised.exception.problem, problem)


class IntegersAndIdsTest(ValidatorTestCase):
    def test_limit_and_offset_are_bounded_integers(self):
        self.assertEqual(v.validate_limit(1), 1)
        self.assertEqual(v.validate_limit(limits.MAX_LIST_LIMIT), limits.MAX_LIST_LIMIT)
        self.assertEqual(v.validate_offset(0), 0)
        self.assertEqual(
            v.validate_offset(limits.MAX_LIST_OFFSET), limits.MAX_LIST_OFFSET
        )
        cases = [
            (v.validate_limit, True, InputProblem.NOT_AN_INTEGER),
            (v.validate_limit, 1.0, InputProblem.NOT_AN_INTEGER),
            (v.validate_limit, "5", InputProblem.NOT_AN_INTEGER),
            (v.validate_limit, None, InputProblem.NOT_AN_INTEGER),
            (v.validate_limit, 0, InputProblem.OUT_OF_RANGE),
            (v.validate_limit, -1, InputProblem.OUT_OF_RANGE),
            (v.validate_limit, limits.MAX_LIST_LIMIT + 1, InputProblem.OUT_OF_RANGE),
            (v.validate_offset, False, InputProblem.NOT_AN_INTEGER),
            (v.validate_offset, -1, InputProblem.OUT_OF_RANGE),
            (v.validate_offset, limits.MAX_LIST_OFFSET + 1, InputProblem.OUT_OF_RANGE),
        ]
        for function, value, problem in cases:
            with self.subTest(function=function.__name__, value=repr(value)):
                self.assertRefused(function, value, problem)

    def test_a_uuid_is_a_uuid_or_its_canonical_string(self):
        value = uuid.uuid4()
        self.assertIs(v.validate_uuid("id", value), value)
        self.assertEqual(v.validate_uuid("id", str(value)), value)
        for bad in (
            str(value).upper(),
            value.hex,
            f"{{{value}}}",
            f"urn:uuid:{value}",
            None,
            5,
            b"x",
            "",
        ):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    v.validate_uuid("id", bad)
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem),
                    ("id", InputProblem.NOT_A_UUID),
                )

    def test_bool_arguments_are_bools(self):
        self.assertIs(v.validate_bool("private", True), True)
        self.assertIs(v.validate_bool("private", False), False)
        for bad in (0, 1, "true", None, "yes"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    v.validate_bool("private", bad)
                self.assertEqual(raised.exception.problem, InputProblem.NOT_A_BOOL)

    def test_project_ids_are_a_bounded_collection_of_uuids(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        self.assertEqual(
            v.validate_project_ids([first, str(second), first]), (first, second)
        )
        self.assertEqual(v.validate_project_ids(()), ())
        many = [uuid.uuid4() for _ in range(limits.MAX_PURGE_PROJECTS)]
        self.assertEqual(len(v.validate_project_ids(many)), limits.MAX_PURGE_PROJECTS)
        cases = [
            (str(first), InputProblem.NOT_A_COLLECTION),
            (None, InputProblem.NOT_A_COLLECTION),
            (5, InputProblem.NOT_A_COLLECTION),
            ([first, "nope"], InputProblem.NOT_A_UUID),
            (many + [uuid.uuid4()], InputProblem.TOO_MANY),
            # Duplicates count too: the loop over a hostile iterable is bounded.
            ([first] * (limits.MAX_PURGE_PROJECTS + 1), InputProblem.TOO_MANY),
        ]
        for value, problem in cases:
            with self.subTest(problem=problem, size=type(value).__name__):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    v.validate_project_ids(value)
                self.assertEqual(raised.exception.problem, problem)

    def test_a_generator_that_never_ends_is_stopped(self):
        def endless():
            while True:
                yield uuid.UUID(int=1)

        with self.assertRaises(InvalidRepositoryInputError) as raised:
            v.validate_project_ids(endless())
        self.assertEqual(raised.exception.problem, InputProblem.TOO_MANY)


if __name__ == "__main__":
    unittest.main()
