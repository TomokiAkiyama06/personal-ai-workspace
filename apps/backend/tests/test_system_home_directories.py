"""The protected homes of the projection and the recovery checkout (issue #309).

``pwd.getpwall()`` also returns systemd DynamicUsers through ``nss-systemd``
(``chrony-wait``, uid 64847, home ``/``). A home of ``/`` contains every path,
so it made every projection and recovery directory "overlap a home". ``/`` is
not a home; ``/home`` and the real accounts' homes stay protected (fail closed).
Every test writes below its own ``tempfile`` directory only.
"""

import os
import pwd
import unittest
from unittest import mock

from paw_backend.memory.projection import (
    ProjectionTargetError,
    TargetProblem,
    open_target,
    system_home_directories,
)
from paw_backend.recovery.files import (
    CheckoutProblem,
    RecoveryFilesError,
    check_directory_path,
)

from .projection_support import TemporaryRoot


def _entry(name: str, uid: int, home: str) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", uid, uid, "", home, "/usr/sbin/nologin"))


class SystemHomeDirectoriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryRoot(self)
        self.entries = [
            _entry("root", 0, "/root"),
            _entry("alice", 1000, "/home/alice"),
            _entry("someone", 1001, str(self.tmp.home)),
            # A systemd DynamicUser, as nss-systemd reports it (issue #309).
            _entry("chrony-wait", 64847, "/"),
            _entry("nobody", 65534, "/nonexistent"),
        ]
        patcher = mock.patch("pwd.getpwall", return_value=self.entries)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_filesystem_root_is_not_a_home(self):
        homes = system_home_directories()
        self.assertNotIn("/", homes)
        self.assertEqual(
            homes,
            tuple(sorted({"/home", "/home/alice", str(self.tmp.home), "/nonexistent"})),
        )

    def test_a_home_that_resolves_to_the_root_is_not_a_home(self):
        link = self.tmp.base / "rootlink"
        link.symlink_to("/")
        self.entries.append(_entry("linked", 1002, str(link)))
        self.assertNotIn(str(link), system_home_directories())

    def test_the_accounts_below_min_uid_are_still_ignored(self):
        self.assertNotIn("/root", system_home_directories())
        self.assertNotIn("/home/alice", system_home_directories(1001))

    def test_the_projection_directory_is_accepted(self):
        target = open_target(self.tmp.root, system_home_directories())
        target.close()
        self.assertTrue(self.tmp.root.is_dir())

    def test_the_recovery_checkout_is_accepted(self):
        checkout = self.tmp.base / "recovery"
        checkout.mkdir()
        self.assertEqual(
            check_directory_path(checkout, system_home_directories()), str(checkout)
        )

    def test_a_directory_in_or_above_a_real_home_is_still_refused(self):
        homes = system_home_directories()
        for path in (self.tmp.home / "memory", self.tmp.home, self.tmp.home.parent):
            with self.assertRaises(ProjectionTargetError) as caught:
                open_target(path, homes)
            self.assertEqual(caught.exception.problem, TargetProblem.OVERLAPS_HOME)
        for path in ("/home/alice/recovery", "/home/alice", "/home", "/home/bob"):
            with self.assertRaises(RecoveryFilesError) as refused:
                check_directory_path(path, homes)
            self.assertEqual(refused.exception.problem, CheckoutProblem.OVERLAPS_HOME)
        self.assertEqual(os.listdir(self.tmp.home), [])


if __name__ == "__main__":
    unittest.main()
