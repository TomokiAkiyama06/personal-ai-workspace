"""The words the sandbox's ``git`` shim accepts (Decision 0083, 2): ``gitargs.py``.

Every option is rewritten to one spelling or refused; revisions are only
``HEAD``, ``HEAD~<n>``, the base ref and full object ids; anything else that is
not an option or a revision is a path (checked later, on the host).
"""

import unittest

from paw_backend.sandbox.gitargs import (
    CANONICAL_OPTIONS,
    GitReadRefused,
    GitRefusal,
    Revision,
    canonical_revision,
    is_canonical_option,
    parse_git_read,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "89abcdef0123456789abcdef0123456789abcdef"


class SubcommandTest(unittest.TestCase):
    def refused(self, argv, code):
        with self.assertRaises(GitReadRefused) as caught:
            parse_git_read(argv)
        self.assertIs(caught.exception.code, code)

    def test_only_the_four_reads_are_accepted(self):
        for name in ("status", "diff", "log", "show"):
            self.assertEqual(parse_git_read([name]).subcommand, name)
        for argv in (
            ["commit", "-m", "x"],
            ["checkout", "main"],
            ["reset", "--hard"],
            ["push"],
            ["fetch"],
            ["config", "user.name"],
            ["cat-file", "-p", SHA],
            ["rev-parse", "HEAD"],
            ["worktree", "list"],
            ["add", "."],
            [],
        ):
            self.refused(argv, GitRefusal.SUBCOMMAND)

    def test_global_options_are_refused_but_no_pager(self):
        self.assertEqual(parse_git_read(["--no-pager", "diff"]).subcommand, "diff")
        self.assertEqual(parse_git_read(["-P", "log"]).subcommand, "log")
        for argv in (
            ["-c", "core.pager=sh", "diff"],
            ["-C", "/tmp", "status"],
            ["--git-dir=/x", "status"],
            ["--work-tree=/x", "status"],
            ["--exec-path=/x", "log"],
        ):
            self.refused(argv, GitRefusal.SUBCOMMAND)

    def test_too_many_or_too_long_words(self):
        self.refused(["log", *(["--stat"] * 64)], GitRefusal.TOO_LONG)
        self.refused(["diff", "a" * 1025], GitRefusal.TOO_LONG)
        self.refused(["diff", "a\x00b"], GitRefusal.OPTION)
        self.refused("diff", GitRefusal.SUBCOMMAND)


class OptionTest(unittest.TestCase):
    def options(self, *argv):
        return parse_git_read(list(argv)).options

    def test_spellings_become_one(self):
        self.assertEqual(self.options("status", "-s", "-b"), ("--short", "--branch"))
        self.assertEqual(self.options("status", "--porcelain"), ("--porcelain=v1",))
        self.assertEqual(self.options("status", "-uno"), ("--untracked-files=no",))
        self.assertEqual(
            self.options("diff", "--staged", "-p"), ("--cached", "--patch")
        )
        self.assertEqual(self.options("diff", "-U5"), ("--unified=5",))
        self.assertEqual(self.options("log", "-n", "5"), ("--max-count=5",))
        self.assertEqual(self.options("log", "-n5"), ("--max-count=5",))
        self.assertEqual(self.options("log", "-5"), ("--max-count=5",))
        self.assertEqual(self.options("log", "--max-count", "7"), ("--max-count=7",))
        self.assertEqual(self.options("log", "--pretty=oneline"), ("--format=oneline",))
        self.assertEqual(self.options("log", "--pretty"), ("--format=medium",))
        self.assertEqual(self.options("show", "-s"), ("--no-patch",))
        # The same option twice is sent once.
        self.assertEqual(self.options("diff", "--stat", "--stat"), ("--stat",))

    def test_what_the_backend_adds_is_dropped(self):
        self.assertEqual(self.options("diff", "--no-color", "--no-ext-diff"), ())
        self.assertEqual(self.options("log", "--no-textconv"), ())

    def test_dangerous_options_are_refused(self):
        for argv in (
            ["diff", "--no-index", "a", "b"],
            ["diff", "-O/etc/passwd"],
            ["diff", "--output=/tmp/x"],
            ["diff", "--ext-diff"],
            ["diff", "--textconv"],
            ["log", "--format=%H%x00%B"],
            ["log", "--pretty=format:%s"],
            ["show", "--format=raw"],
            ["log", "--decorate"],
            ["log", "--all"],
            ["log", "--branches"],
            ["log", "--glob=refs/*"],
            ["status", "--ignored"],
            ["status", "--ignore-submodules=none"],
            ["diff", "--color=always"],
            ["log", "-n"],
            ["log", "-n", "x"],
            ["log", "--max-count=1000001"],
            ["diff", "-U99999"],
            ["log", "--exec=x"],
            ["show", "--show-signature"],
            ["log", "--follow", "a.py"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(GitReadRefused) as caught:
                    parse_git_read(argv)
                self.assertIs(caught.exception.code, GitRefusal.OPTION)

    def test_every_canonical_option_is_recognised(self):
        for subcommand, options in CANONICAL_OPTIONS.items():
            for option in options:
                self.assertTrue(is_canonical_option(subcommand, option), option)
        self.assertTrue(is_canonical_option("log", "--max-count=3"))
        self.assertTrue(is_canonical_option("log", "--format=fuller"))
        self.assertFalse(is_canonical_option("log", "--format=%H"))
        self.assertFalse(is_canonical_option("diff", "--max-count=3"))
        self.assertFalse(is_canonical_option("status", "--patch"))


class RevisionTest(unittest.TestCase):
    def test_the_revision_grammar(self):
        self.assertEqual(canonical_revision("HEAD"), "HEAD")
        self.assertEqual(canonical_revision("HEAD~"), "HEAD~1")
        self.assertEqual(canonical_revision("HEAD~3"), "HEAD~3")
        self.assertEqual(canonical_revision("HEAD^"), "HEAD~1")
        self.assertEqual(canonical_revision("HEAD^^"), "HEAD~2")
        self.assertEqual(canonical_revision("HEAD~0"), "HEAD")
        self.assertEqual(canonical_revision(SHA), SHA)
        self.assertEqual(
            canonical_revision("refs/paw/base/paw/t/1/a"), "refs/paw/base/paw/t/1/a"
        )
        # A reviewer's review base (Decision 0099, 5).
        review = "refs/paw/review-base/paw/t/1/paw-review-claude"
        self.assertEqual(canonical_revision(review), review)
        self.assertEqual(
            parse_git_read(["diff", f"{review}..HEAD"]).revisions,
            (Revision(review, negated=True), Revision("HEAD")),
        )
        for word in (
            "refs/paw/review-base/../x",
            "refs/paw/review/paw/t/1/a",
            "main",
            "origin/main",
            "HEAD@{1}",
            "abc1234",
            SHA.upper(),
            "refs/heads/main",
            "refs/paw/base/../x",
            "HEAD~99999",
            "@",
        ):
            self.assertIsNone(canonical_revision(word), word)

    def test_revisions_and_ranges(self):
        parsed = parse_git_read(["diff", f"{SHA}..HEAD"])
        self.assertEqual(
            parsed.revisions, (Revision(SHA, negated=True), Revision("HEAD"))
        )
        self.assertEqual(
            parse_git_read(["log", f"^{SHA}", "HEAD~2"]).revisions,
            (Revision(SHA, negated=True), Revision("HEAD~2")),
        )
        self.assertEqual(
            parse_git_read(["log", "HEAD~2.."]).revisions,
            (Revision("HEAD~2", negated=True), Revision("HEAD")),
        )
        self.assertEqual(
            parse_git_read(["show", SHA, OTHER]).positive,
            (Revision(SHA), Revision(OTHER)),
        )

    def test_reading_objects_by_path_and_other_forms_are_refused(self):
        for argv in (
            ["show", "HEAD:secret.txt"],
            ["show", f"{SHA}:a/b"],
            ["diff", "HEAD...main"],
            ["log", "main.."],
            ["show", "HEAD@{1}"],
            ["show", "abc1234"],
            ["show", f"{SHA}^{{tree}}"],
            ["diff", f"^{SHA}"],
            ["show", f"{SHA}..HEAD"],
            ["diff", "HEAD", "HEAD~1", "HEAD~2"],
            ["show", *(["HEAD"] * 5)],
            ["show", "ABC1234"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(GitReadRefused) as caught:
                    parse_git_read(argv)
                self.assertIs(caught.exception.code, GitRefusal.REVISION)

    def test_other_words_are_paths(self):
        parsed = parse_git_read(["diff", "--stat", "src/a.py", "README.md"])
        self.assertEqual(parsed.paths, ("src/a.py", "README.md"))
        self.assertEqual(parsed.revisions, ())
        # A branch name is a path to the parser (a literal path that matches
        # nothing in the work tree).
        self.assertEqual(parse_git_read(["log", "origin/main"]).paths, ("origin/main",))
        # After ``--`` every word is a path, even one that looks like an option.
        parsed = parse_git_read(["log", "HEAD", "--", "--stat", "HEAD"])
        self.assertEqual(parsed.paths, ("--stat", "HEAD"))
        self.assertEqual(parsed.options, ())
        # ``status`` takes no revision: a word is a path there.
        self.assertEqual(parse_git_read(["status", "HEAD"]).paths, ("HEAD",))


if __name__ == "__main__":
    unittest.main()
