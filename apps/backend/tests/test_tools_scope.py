import asyncio
import os
import pathlib
import tempfile
import unittest

from paw_backend.authz import ProjectState
from paw_backend.tools import (
    LexicalPathResolver,
    RealpathResolver,
    ScopeStatus,
    TargetError,
    TaskScope,
)
from paw_backend.tools.scope import (
    PathResolutionError,
    Target,
    TargetKind,
    classify_targets,
    normalise_host,
    normalise_path,
    normalise_project,
    normalise_url,
    path_within,
)

from .tools_support import HANDLE, OTHER_HANDLE, P1, P2, ROOT, DictResolver, make_scope

BASE = ROOT


class PathNormalisationTest(unittest.TestCase):
    def test_absolute_and_relative_paths_get_one_canonical_form(self):
        cases = {
            f"{ROOT}/a.py": f"{ROOT}/a.py",
            "a.py": f"{ROOT}/a.py",
            "./a//b/./c.py": f"{ROOT}/a/b/c.py",
            f"{ROOT}//a///b/": f"{ROOT}/a/b",
            "dir/": f"{ROOT}/dir",
            ".hidden/.git": f"{ROOT}/.hidden/.git",
            "a b/c d": f"{ROOT}/a b/c d",
            "é/ü.py": f"{ROOT}/é/ü.py",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise_path(raw, base=BASE), expected)

    def test_parent_directory_segments_are_refused_not_collapsed(self):
        # Collapsing "link/.." lexically would disagree with the file system
        # when "link" is a symlink; so no ".." is ever accepted.
        for raw in (
            "../x",
            "a/../../etc/passwd",
            f"{ROOT}/../etc",
            "a/..",
            "..",
            "...",
            "a/.../b",
            ". .",
            "a/ ../b",
            "a/.. /b",
            "a/ .. /b",
            f"{ROOT}/link/../../x",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_path(raw, base=BASE)

    def test_encoded_and_alternative_separators_are_refused(self):
        for raw in (
            "a\\b",
            "..\\..\\x",
            "%2e%2e/x",
            "%2E%2E%2Fx",
            "a%2fb",
            "a%5Cb",
            "a%5cb",
            "~/x",
            "~root/x",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_path(raw, base=BASE)

    def test_a_percent_sign_that_is_not_a_separator_is_an_ordinary_name(self):
        self.assertEqual(normalise_path("100%/a", base=BASE), f"{ROOT}/100%/a")
        self.assertEqual(normalise_path("a%20b", base=BASE), f"{ROOT}/a%20b")

    def test_control_format_and_look_alike_characters_are_refused(self):
        for raw in (
            "a\x00b",
            "a\nb",
            "a\tb",
            "a\x7fb",
            "a​b",  # zero width space
            "‮gnp.py",  # right-to-left override
            "a b",  # no-break space
            "a b",  # line separator
            "﻿a",
            "．．/etc",  # full-width ".."
            "a／b",  # full-width "/"
            "é.py",  # decomposed (not NFKC-stable)
            "ﬁle",  # "fi" ligature
            "a\ud800b",  # lone surrogate
            "a\U000e0001b",  # tag character
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_path(raw, base=BASE)

    def test_only_real_text_of_bounded_length_is_accepted(self):
        for raw in ("", "x" * 1025, None, 5, b"/a", pathlib.PurePosixPath("/a"), ["a"]):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_path(raw, base=BASE)
        self.assertEqual(
            normalise_path("/" + "x" * 1023), "/" + "x" * 1023
        )  # the limit is on characters

    def test_a_relative_path_needs_an_absolute_base(self):
        for base in (None, "relative"):
            with self.subTest(base=base):
                with self.assertRaises(TargetError):
                    normalise_path("a.py", base=base)

    def test_a_str_subclass_is_not_accepted(self):
        class Sneaky(str):
            pass

        with self.assertRaises(TargetError):
            normalise_path(Sneaky("/a"))

    def test_the_root_itself_normalises_to_slash(self):
        self.assertEqual(normalise_path("/"), "/")
        self.assertEqual(normalise_path("///"), "/")


class ContainmentTest(unittest.TestCase):
    def test_inside_the_root(self):
        self.assertTrue(path_within(ROOT, ROOT))
        self.assertTrue(path_within(f"{ROOT}/a", ROOT))
        self.assertTrue(path_within(f"{ROOT}/a/b/c", ROOT))

    def test_a_sibling_with_the_same_prefix_is_outside(self):
        self.assertFalse(path_within(f"{ROOT}-evil/a", ROOT))
        self.assertFalse(path_within(f"{ROOT}2", ROOT))
        self.assertFalse(path_within("/srv/paw-test", ROOT))
        self.assertFalse(path_within("/", ROOT))

    def test_the_comparison_is_case_sensitive(self):
        # On a case-insensitive file system this is a false denial; it can
        # never be an escape.
        self.assertFalse(path_within(ROOT.upper() + "/a", ROOT))
        self.assertFalse(path_within(ROOT.replace("worktree", "Worktree") + "/a", ROOT))

    def test_the_file_system_root_contains_nothing(self):
        self.assertFalse(path_within("/etc/passwd", "/"))
        self.assertFalse(path_within("/", "/"))


class HostNormalisationTest(unittest.TestCase):
    def test_canonical_forms(self):
        cases = {
            "github.com": "github.com",
            "GitHub.COM": "github.com",
            "github.com.": "github.com",
            "API.GitHub.com": "api.github.com",
            "xn--r8jz45g.jp": "xn--r8jz45g.jp",
            "localhost": "localhost",
            "127.0.0.1": "127.0.0.1",
            "[::1]": "[::1]",
            "[0:0:0:0:0:0:0:1]": "[::1]",
            "[2001:DB8::1]": "[2001:db8::1]",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise_host(raw), expected)

    def test_tricks_are_refused(self):
        for raw in (
            "",
            " github.com",
            "github.com ",
            "a b",
            "exa_mple.com",
            "-a.com",
            "a-.com",
            "a..com",
            ".com",
            "例え.jp",  # must be given as xn-- punycode
            "githuб.com",  # a Cyrillic letter that looks Latin
            "github.com​",
            "127.1",  # short form of 127.0.0.1
            "0x7f.0.0.1",
            "2130706433",  # decimal form
            "0177.0.0.1",  # octal
            "01.2.3.4",
            "1.2.3",
            "256.1.1.1",
            "[::1%eth0]",
            "[::1",
            "::1",
            "host/x",
            "a@b",
            "a:80",
            "%67ithub.com",
            "a" * 64 + ".com",
            "x" * 300,
            None,
            5,
            b"github.com",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_host(raw)

    def test_a_look_alike_host_is_a_different_host(self):
        self.assertNotEqual(normalise_host("github.com.evil.com"), "github.com")
        self.assertNotEqual(normalise_host("evilgithub.com"), "github.com")
        self.assertNotEqual(normalise_host("github.co"), "github.com")


class UrlNormalisationTest(unittest.TestCase):
    def test_canonical_forms(self):
        cases = {
            "https://GitHub.com:443/a/b?x=1#frag": (
                "https://github.com/a/b?x=1",
                "github.com",
            ),
            "HTTP://github.com:80": ("http://github.com", "github.com"),
            "https://api.github.com/repos": (
                "https://api.github.com/repos",
                "api.github.com",
            ),
            "https://github.com/a%20b?q=%2e%2e": (
                "https://github.com/a%20b?q=%2e%2e",
                "github.com",
            ),
            "https://[::1]:443/x": ("https://[::1]/x", "[::1]"),
            "https://github.com.evil.com/": (
                "https://github.com.evil.com/",
                "github.com.evil.com",
            ),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise_url(raw), expected)

    def test_tricks_are_refused(self):
        for raw in (
            "ftp://github.com/x",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "gopher://github.com",
            "https://user:pw@github.com/",
            "https://github.com@evil.com/",
            "https://evil.com\\@github.com/",
            "https://github.com\\.evil.com/",
            "https://github.com:8443/",
            "https://github.com:0/",
            "https://github.com:abc/",
            "https://github.com:443443/",
            "https://a:b:c/",
            "https:/github.com",
            "https:github.com",
            "//github.com/x",
            "github.com/x",
            "https://",
            "https:///x",
            "https://github.com/a b",
            "https://github.com/a\nb",
            "https://github.com/é",
            "https://[::1/",
            "https://[::1]x/",
            " https://github.com/",
            "https://exa mple.com/",
            "https://127.1/",
            "https://" + "a" * 2050,
            None,
            5,
        ):
            with self.subTest(raw=raw[:60] if isinstance(raw, str) else raw):
                with self.assertRaises(TargetError):
                    normalise_url(raw)


class ProjectNormalisationTest(unittest.TestCase):
    def test_only_canonical_uuids(self):
        self.assertEqual(normalise_project(str(P1)), P1)
        self.assertEqual(normalise_project(P1), P1)
        letters = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        self.assertEqual(str(normalise_project(letters)), letters)
        for raw in (
            letters.upper(),
            letters.replace("-", ""),
            "{" + letters + "}",
            "p1",
            "",
            None,
            5,
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(TargetError):
                    normalise_project(raw)


class TaskScopeTest(unittest.TestCase):
    def test_a_scope_is_normalised_when_it_is_built(self):
        scope = TaskScope(
            path_roots=[f"{ROOT}/", ROOT, "/srv/other//tree"],
            hosts=["GitHub.com", "github.com."],
            projects={str(P1): "active"},
            credential_handles=[HANDLE, HANDLE],
        )
        self.assertEqual(scope.path_roots, (ROOT, "/srv/other/tree"))
        self.assertEqual(scope.hosts, frozenset({"github.com"}))
        self.assertEqual(dict(scope.projects), {P1: ProjectState.ACTIVE})
        self.assertEqual(scope.credential_handles, frozenset({HANDLE}))

    def test_invalid_scopes_are_refused(self):
        for overrides in (
            {"path_roots": ["/"]},
            {"path_roots": ["relative/root"]},
            {"path_roots": [f"{ROOT}/.."]},
            {"path_roots": ["~"]},
            {"hosts": ["*.github.com"]},
            {"hosts": ["https://github.com"]},
            {"projects": {"not-a-uuid": ProjectState.ACTIVE}},
            {"projects": {P1: "deleted"}},
            {"credential_handles": ["ghp_" + "a" * 36]},
            {"credential_handles": ["cred_" + "A" * 32]},
            {"path_roots": [f"/srv/{i}" for i in range(33)]},
            {"hosts": [f"h{i}.example.com" for i in range(129)]},
        ):
            with self.subTest(overrides=str(overrides)[:60]):
                with self.assertRaises((ValueError, TypeError)):
                    make_scope(**overrides)

    def test_a_bare_string_is_not_a_collection(self):
        for overrides in (
            {"path_roots": ROOT},
            {"hosts": "github.com"},
            {"credential_handles": HANDLE},
        ):
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(TypeError):
                    make_scope(**overrides)

    def test_a_scope_cannot_be_changed(self):
        scope = make_scope()
        with self.assertRaises(TypeError):
            scope.projects[P2] = ProjectState.ACTIVE
        with self.assertRaises(AttributeError):
            scope.hosts.add("evil.com")
        with self.assertRaises(AttributeError):
            scope.path_roots = ("/",)


class ClassifyTest(unittest.IsolatedAsyncioTestCase):
    async def classify(self, targets, scope=None, resolver=None, **kw):
        return await classify_targets(
            targets, scope or make_scope(), resolver or LexicalPathResolver(), **kw
        )

    def path(self, value):
        return Target(TargetKind.PATH, value)

    async def test_everything_inside_the_scope(self):
        result = await self.classify(
            [
                self.path(f"{ROOT}/a.py"),
                Target(TargetKind.HOST, "github.com"),
                Target(TargetKind.PROJECT, str(P1)),
                Target(TargetKind.CREDENTIAL, HANDLE),
            ]
        )
        self.assertEqual(
            (result.status, result.offending), (ScopeStatus.IN_SCOPE, None)
        )

    async def test_no_targets_is_in_scope(self):
        result = await self.classify([])
        self.assertIs(result.status, ScopeStatus.IN_SCOPE)

    async def test_a_path_outside_the_root(self):
        for value in (
            "/etc/passwd",
            f"{ROOT}-evil/x",
            "/srv/paw-test",
            ROOT.upper() + "/a",
            "/",
        ):
            with self.subTest(value=value):
                result = await self.classify([self.path(value)])
                self.assertEqual(
                    (result.status, result.offending),
                    (ScopeStatus.OUT_OF_SCOPE, TargetKind.PATH),
                )

    async def test_a_host_outside_the_task_hosts_is_only_host_out_of_scope(self):
        for host in (
            "evil.com",
            "github.com.evil.com",
            "evilgithub.com",
            "gist.github.com",
        ):
            with self.subTest(host=host):
                result = await self.classify([Target(TargetKind.HOST, host)])
                self.assertEqual(
                    (result.status, result.offending),
                    (ScopeStatus.HOST_OUT_OF_SCOPE, TargetKind.HOST),
                )

    async def test_a_project_or_credential_outside_the_scope(self):
        result = await self.classify([Target(TargetKind.PROJECT, str(P2))])
        self.assertEqual(
            (result.status, result.offending),
            (ScopeStatus.OUT_OF_SCOPE, TargetKind.PROJECT),
        )
        result = await self.classify([Target(TargetKind.CREDENTIAL, OTHER_HANDLE)])
        self.assertEqual(
            (result.status, result.offending),
            (ScopeStatus.OUT_OF_SCOPE, TargetKind.CREDENTIAL),
        )

    async def test_a_target_that_is_out_of_scope_beats_a_host_that_is(self):
        result = await self.classify(
            [Target(TargetKind.HOST, "evil.com"), self.path("/etc/passwd")]
        )
        self.assertEqual(
            (result.status, result.offending),
            (ScopeStatus.OUT_OF_SCOPE, TargetKind.PATH),
        )

    async def test_a_symlink_that_leaves_the_root_is_outside(self):
        resolver = DictResolver({f"{ROOT}/link": "/etc"})
        result = await self.classify(
            [self.path(f"{ROOT}/link/passwd")], resolver=resolver
        )
        self.assertEqual(result.status, ScopeStatus.OUT_OF_SCOPE)

    async def test_a_symlink_that_stays_inside_is_fine(self):
        resolver = DictResolver({f"{ROOT}/link": f"{ROOT}/real"})
        result = await self.classify(
            [self.path(f"{ROOT}/link/file")], resolver=resolver
        )
        self.assertEqual(result.status, ScopeStatus.IN_SCOPE)

    async def test_the_roots_are_resolved_too(self):
        # The worktree itself lives behind a symlink (ROOT -> /data/wt).
        resolver = DictResolver({ROOT: "/data/wt"})
        result = await self.classify([self.path(f"{ROOT}/a.py")], resolver=resolver)
        self.assertEqual(result.status, ScopeStatus.IN_SCOPE)
        result = await self.classify(
            [self.path("/data/wt-other/a.py")], resolver=resolver
        )
        self.assertEqual(result.status, ScopeStatus.OUT_OF_SCOPE)

    async def test_a_root_that_resolves_to_the_file_system_root_contains_nothing(self):
        resolver = DictResolver({ROOT: "/"})
        result = await self.classify([self.path(f"{ROOT}/a.py")], resolver=resolver)
        self.assertEqual(result.status, ScopeStatus.OUT_OF_SCOPE)

    async def test_a_resolver_failure_is_reported_by_type_only(self):
        class Broken:
            async def resolve(self, path):
                raise OSError("/secret/mount/point is unreadable")

        with self.assertRaises(PathResolutionError) as caught:
            await self.classify([self.path(f"{ROOT}/a")], resolver=Broken())
        self.assertEqual(str(caught.exception), "OSError")

    async def test_an_unusable_resolver_answer_is_a_failure(self):
        for answer in ("relative/path", "/a/../b", "", None, 5, f"{ROOT}/a\x00"):
            with self.subTest(answer=answer):

                class Odd:
                    async def resolve(self, path, answer=answer):
                        return answer

                with self.assertRaises(PathResolutionError):
                    await self.classify([self.path(f"{ROOT}/a")], resolver=Odd())

    async def test_a_resolver_that_never_answers_times_out(self):
        class Stuck:
            async def resolve(self, path):
                await asyncio.Event().wait()

        with self.assertRaises(PathResolutionError) as caught:
            await self.classify(
                [self.path(f"{ROOT}/a")], resolver=Stuck(), timeout_seconds=0.05
            )
        self.assertEqual(str(caught.exception), "TimeoutError")

    async def test_the_resolver_is_not_used_when_no_path_is_touched(self):
        resolver = DictResolver()
        await self.classify([Target(TargetKind.HOST, "github.com")], resolver=resolver)
        self.assertEqual(resolver.calls, [])


class RealSymlinkTest(unittest.IsolatedAsyncioTestCase):
    """A real file system: ``os.path.realpath`` follows the link out of the root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(os.path.realpath(tmp.name))
        self.root = base / "worktree"
        outside = base / "outside"
        (self.root / "real").mkdir(parents=True)
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        (self.root / "real" / "ok.txt").write_text("x")
        os.symlink(outside, self.root / "escape")
        os.symlink(self.root / "real", self.root / "inside")
        self.scope = TaskScope(
            path_roots=[str(self.root)], hosts=[], projects={P1: ProjectState.ACTIVE}
        )

    async def status(self, path):
        result = await classify_targets(
            [Target(TargetKind.PATH, path)], self.scope, RealpathResolver()
        )
        return result.status

    async def test_a_symlink_pointing_outside_the_root_is_denied(self):
        root = self.root
        out = ScopeStatus.OUT_OF_SCOPE
        self.assertEqual(await self.status(f"{root}/escape/secret.txt"), out)
        self.assertEqual(await self.status(f"{root}/escape"), out)
        # a name that does not exist yet, behind the link, is still outside
        self.assertEqual(await self.status(f"{root}/escape/new.txt"), out)

    async def test_paths_that_stay_inside_are_in_scope(self):
        root = self.root
        inside = ScopeStatus.IN_SCOPE
        self.assertEqual(await self.status(f"{root}/inside/ok.txt"), inside)
        self.assertEqual(await self.status(f"{root}/real/ok.txt"), inside)
        # a file that does not exist yet (about to be created) resolves lexically
        self.assertEqual(await self.status(f"{root}/new/file.txt"), inside)


if __name__ == "__main__":
    unittest.main()
