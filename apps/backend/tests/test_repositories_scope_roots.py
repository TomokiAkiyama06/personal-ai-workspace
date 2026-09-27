"""``scope_entries`` verifies every checkout root before it derives nesting (PAW-027).

The Tool Broker resolves each repository root with ``realpath`` when a tool is
called, so the nesting the orchestrator hands it must come from the same directories,
not from the paths that were stored at registration. A user (or an agent working in
the user's account) can replace a registered checkout with a symbolic link, or rename
another checkout into its place, after registration. If ``scope_entries`` trusted the
stored paths, repository A's scope would omit repository B although calls through A
now resolve into B, and B's stricter ACL would never apply. These tests replace real
directories with real links and renames and assert that the scope is refused
(fail closed) with a typed error, and that an unchanged tree still works.

Real PostgreSQL and real git on temporary repositories. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import os
import shutil
from unittest import mock

from paw_backend.authz.roles import ProjectRole
from paw_backend.repositories import (
    CheckoutChangedError,
    CheckoutNotFoundError,
    PathProblem,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)
from .test_repositories_paths import foreign_owner


@requires_postgres
@requires_git
class ScopeRootsTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.manager = self.seed_user_with_account("alice")
        self.seed_manager(self.project_id, self.manager)
        self.home = self.account(self.manager).home
        self.alice = self.actor(self.manager)

    async def register(self, relative: str, name: str):
        """Register an existing repository of Alice's; ``(repository id, path)``."""
        path = f"{self.home}/{relative}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.world.make_repository(path)
        result = await self.service.register_existing(
            self.alice, self.project_id, path, name=name
        )
        return result.repository.id, path

    async def scope(self, repository_id):
        return await self.service.scope_entries(
            self.manager, self.project_id, repository_id
        )

    def ids(self, entries):
        return [entry.repo_id for entry in entries]

    async def assertRefused(self, repository_id, problem=None):
        with self.assertLogs("paw_backend.repositories.service", "WARNING"):
            with self.assertRaises(CheckoutChangedError) as raised:
                await self.scope(repository_id)
        if problem is not None:
            self.assertIs(raised.exception.problem, problem)
        # The message names the reason only, never a path.
        self.assertNotIn(self.world.root, str(raised.exception))
        return raised.exception


class UnchangedTreeTest(ScopeRootsTestCase):
    async def test_an_unchanged_tree_still_gives_the_nested_scopes(self):
        outer, outer_path = await self.register("src/outer", "outer")
        inner, _ = await self.register("src/outer/vendor/inner", "inner")
        sibling, _ = await self.register("src/outer-two", "sibling")

        self.assertEqual(self.ids(await self.scope(outer)), [outer, inner])
        self.assertEqual(self.ids(await self.scope(inner)), [inner, outer])
        self.assertEqual(self.ids(await self.scope(sibling)), [sibling])
        (first, *_) = await self.scope(outer)
        self.assertEqual(first.root, outer_path)

    async def test_the_identity_of_every_root_is_recorded_when_it_becomes_ready(self):
        repository, path = await self.register("src/tool", "tool")
        (row,) = self.checkout_rows(repository)
        info = os.lstat(path)
        self.assertEqual(
            (int(row["root_device"]), int(row["root_inode"])),
            (info.st_dev, info.st_ino),
        )
        created = await self.service.create_local(self.alice, self.project_id, "fresh")
        (fresh,) = self.checkout_rows(created.repository.id)
        info = os.lstat(created.checkout.path)
        self.assertEqual(
            (int(fresh["root_device"]), int(fresh["root_inode"])),
            (info.st_dev, info.st_ino),
        )
        self.assertEqual(created.checkout.root_inode, info.st_ino)


class ReplacedRootTest(ScopeRootsTestCase):
    async def test_a_checkout_replaced_by_a_link_to_another_is_refused_for_both(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("src/b", "b")
        shutil.rmtree(a_path)
        os.symlink(b_path, a_path)  # calls through A now resolve into B

        error = await self.assertRefused(a, PathProblem.SYMLINK)
        self.assertEqual(error.checkout_id, self.checkout_rows(a)[0]["id"])
        # B is fine itself, but A resolves into it: B's scope cannot be trusted.
        await self.assertRefused(b, PathProblem.CHANGED)

    async def test_another_checkout_renamed_into_place_is_refused_for_both(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("src/b", "b")
        os.rename(a_path, f"{a_path}.old")
        os.rename(b_path, a_path)  # B's directory now sits where A was registered

        await self.assertRefused(a, PathProblem.CHANGED)  # not the directory of A
        await self.assertRefused(b, PathProblem.NOT_FOUND)

    async def test_a_new_directory_at_the_same_path_is_not_the_checkout(self):
        a, a_path = await self.register("src/a", "a")
        replacement = f"{self.home}/replacement"
        os.makedirs(replacement)  # made while A still exists: another inode for sure
        os.rename(a_path, f"{a_path}.old")
        os.rename(replacement, a_path)  # same path, another directory

        await self.assertRefused(a, PathProblem.CHANGED)

    async def test_a_deleted_checkout_is_refused(self):
        a, a_path = await self.register("src/a", "a")
        shutil.rmtree(a_path)
        await self.assertRefused(a, PathProblem.NOT_FOUND)

    async def test_a_checkout_that_is_no_longer_a_directory_is_refused(self):
        a, a_path = await self.register("src/a", "a")
        shutil.rmtree(a_path)
        fs.write(a_path, "now a file\n")
        await self.assertRefused(a, PathProblem.NOT_A_DIRECTORY)

    async def test_a_checkout_of_another_owner_is_refused(self):
        a, a_path = await self.register("src/a", "a")
        with foreign_owner(a_path):
            await self.assertRefused(a, PathProblem.NOT_OWNER)

    async def test_a_link_in_the_middle_of_the_path_is_refused(self):
        a, a_path = await self.register("src/x/a", "a")
        elsewhere = f"{self.home}/elsewhere"
        shutil.move(f"{self.home}/src/x", elsewhere)
        os.symlink(elsewhere, f"{self.home}/src/x")
        await self.assertRefused(a, PathProblem.SYMLINK)

    async def test_two_checkouts_that_now_resolve_into_each_other_are_refused(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("src/b", "b")
        os.makedirs(f"{a_path}/sub")
        shutil.rmtree(b_path)
        os.symlink(f"{a_path}/sub", b_path)  # B now resolves *inside* A

        await self.assertRefused(a, PathProblem.CHANGED)
        await self.assertRefused(b, PathProblem.SYMLINK)


class NestedRootTest(ScopeRootsTestCase):
    async def test_a_nested_checkout_moved_out_by_a_link_refuses_the_outer_scope(self):
        outer, outer_path = await self.register("src/outer", "outer")
        inner, _ = await self.register("src/outer/vendor/inner", "inner")
        away = f"{self.home}/away"
        shutil.move(f"{outer_path}/vendor", away)
        os.symlink(away, f"{outer_path}/vendor")  # inner's path resolves elsewhere

        await self.assertRefused(inner, PathProblem.SYMLINK)
        # The stored path of inner still lies inside outer: the outer scope cannot
        # tell what that path means now, so it is refused too.
        await self.assertRefused(outer, PathProblem.CHANGED)

    async def test_a_changed_checkout_that_has_nothing_to_do_with_it_is_ignored(self):
        a, _ = await self.register("src/a", "a")
        c, c_path = await self.register("other/c", "c")
        shutil.rmtree(c_path)
        os.symlink(f"{self.home}/nowhere-registered", c_path)

        self.assertEqual(self.ids(await self.scope(a)), [a])
        await self.assertRefused(c)

    async def test_a_checkout_that_vanished_does_not_block_an_unrelated_scope(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("src/b", "b")
        os.rename(b_path, f"{self.home}/moved-b")  # B is gone from its path only

        self.assertEqual(self.ids(await self.scope(a)), [a])
        await self.assertRefused(b, PathProblem.NOT_FOUND)


class VanishedCheckoutTest(ScopeRootsTestCase):
    """A checkout that vanished from its path may now sit inside the requested root.

    B is renamed from an unrelated path into a (new) directory of A. A's own identity
    is unchanged and B's stored path is far away, yet the Tool Broker now reaches B's
    files through A and would classify them by A's ACL only. A changed or vanished
    checkout therefore has to be *located* under the requested root (by the identity
    recorded when it became ready) before it can be called unrelated.
    """

    async def checkout_id(self, repository_id):
        return self.checkout_rows(repository_id)[0]["id"]

    async def test_a_checkout_renamed_into_a_new_child_of_the_requested_one(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("other/b", "b")
        os.rename(b_path, f"{a_path}/moved")  # B's files are now reachable through A

        error = await self.assertRefused(a, PathProblem.CHANGED)
        self.assertEqual(error.checkout_id, await self.checkout_id(b))
        await self.assertRefused(b, PathProblem.NOT_FOUND)

    async def test_a_checkout_renamed_into_a_deeper_grandchild(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("other/b", "b")
        os.makedirs(f"{a_path}/x/y")
        os.rename(b_path, f"{a_path}/x/y/b")

        error = await self.assertRefused(a, PathProblem.CHANGED)
        self.assertEqual(error.checkout_id, await self.checkout_id(b))

    async def test_the_old_path_of_the_moved_checkout_taken_by_another_directory(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("other/b", "b")
        os.rename(b_path, f"{a_path}/moved")
        os.makedirs(b_path)  # something else at B's old path: "changed", not "gone"

        await self.assertRefused(a, PathProblem.CHANGED)

    async def test_the_old_path_of_the_moved_checkout_replaced_by_a_link(self):
        a, a_path = await self.register("src/a", "a")
        b, b_path = await self.register("other/b", "b")
        os.rename(b_path, f"{a_path}/moved")
        os.symlink(f"{self.home}/nowhere", b_path)

        await self.assertRefused(a, PathProblem.CHANGED)

    async def test_the_error_names_the_checkout_that_was_found_not_just_any_changed_one(
        self,
    ):
        a, a_path = await self.register("src/a", "a")
        far, far_path = await self.register("other/far", "far")
        moved, moved_path = await self.register("other/moved", "moved")
        shutil.rmtree(far_path)  # vanished, and not inside A
        os.rename(moved_path, f"{a_path}/inside")  # this one is inside A

        error = await self.assertRefused(a, PathProblem.CHANGED)
        self.assertEqual(error.checkout_id, await self.checkout_id(moved))

    async def test_a_checkout_that_vanished_far_away_does_not_block_the_scope(self):
        a, a_path = await self.register("src/a", "a")
        c, c_path = await self.register("other/c", "c")
        shutil.rmtree(c_path)

        self.assertEqual(self.ids(await self.scope(a)), [a])

    async def test_a_checkout_moved_elsewhere_outside_the_root_does_not_block_it(self):
        a, a_path = await self.register("src/a", "a")
        c, c_path = await self.register("other/c", "c")
        os.rename(c_path, f"{self.home}/moved-c")
        os.symlink(f"{self.home}/moved-c", f"{a_path}/link")  # not followed

        self.assertEqual(self.ids(await self.scope(a)), [a])
        await self.assertRefused(c, PathProblem.NOT_FOUND)

    async def test_a_link_cycle_in_the_root_does_not_hang_the_search(self):
        a, a_path = await self.register("src/a", "a")
        c, c_path = await self.register("other/c", "c")
        shutil.rmtree(c_path)
        os.symlink(a_path, f"{a_path}/loop")

        self.assertEqual(self.ids(await self.scope(a)), [a])

    async def test_nothing_is_searched_while_every_checkout_is_as_registered(self):
        outer, _ = await self.register("src/outer", "outer")
        inner, _ = await self.register("src/outer/vendor/inner", "inner")
        far, _ = await self.register("other/far", "far")

        with mock.patch(
            "paw_backend.repositories.service.locate_directory_identity",
            side_effect=AssertionError("searched an unchanged tree"),
        ):
            self.assertEqual(self.ids(await self.scope(outer)), [outer, inner])
            self.assertEqual(self.ids(await self.scope(far)), [far])

    async def test_a_root_too_large_to_search_is_refused_not_guessed(self):
        a, a_path = await self.register("src/a", "a")
        c, c_path = await self.register("other/c", "c")
        for index in range(10):
            os.makedirs(f"{a_path}/d{index}")
        shutil.rmtree(c_path)  # it may have been anywhere: A has to be searched

        with mock.patch(
            "paw_backend.repositories.service.MAX_IDENTITY_SCAN_DIRECTORIES", 5
        ):
            error = await self.assertRefused(a, PathProblem.CHANGED)
            self.assertEqual(error.checkout_id, await self.checkout_id(c))
        with mock.patch(
            "paw_backend.repositories.service.MAX_IDENTITY_SCAN_DIRECTORIES", 500
        ):
            self.assertEqual(self.ids(await self.scope(a)), [a])


class ScopeErrorsTest(ScopeRootsTestCase):
    async def test_the_error_is_typed_and_carries_no_path(self):
        a, a_path = await self.register("src/a", "a")
        shutil.rmtree(a_path)
        with self.assertLogs("paw_backend.repositories.service", "WARNING"):
            with self.assertRaises(CheckoutChangedError) as raised:
                await self.scope(a)
        self.assertEqual(raised.exception.code, "checkout_changed")
        self.assertEqual(str(raised.exception), "Checkout changed: not_found")
        self.assertNotIn(a_path, repr(raised.exception.args))

    async def test_the_refusal_is_logged_with_ids_only(self):
        a, a_path = await self.register("src/a", "a")
        shutil.rmtree(a_path)
        with self.assertLogs("paw_backend.repositories.service", "WARNING") as logs:
            with self.assertRaises(CheckoutChangedError):
                await self.scope(a)
        text = "\n".join(logs.output)
        self.assertIn("not_found", text)
        self.assertNotIn(self.world.root, text)

    async def test_a_repository_without_a_ready_checkout_is_still_not_usable(self):
        repository = self.seed_repository(self.project_id, name="nobody")
        with self.assertRaises(CheckoutNotFoundError):
            await self.scope(repository)
        self.seed_checkout(
            repository, self.project_id, self.manager, f"{self.home}/p", state="pending"
        )
        with self.assertRaises(CheckoutNotFoundError):
            await self.scope(repository)

    async def test_a_user_with_no_linux_account_has_no_scope(self):
        from paw_backend.repositories import LinuxAccountUnavailableError

        stranger = self.seed_user()
        self.seed_member(self.project_id, stranger, ProjectRole.CONTRIBUTOR)
        repository = self.seed_repository(self.project_id, name="x")
        self.seed_checkout(repository, self.project_id, stranger, "/nowhere")
        with self.assertRaises(LinuxAccountUnavailableError):
            await self.service.scope_entries(stranger, self.project_id, repository)

    async def test_more_checkouts_than_the_limit_are_refused_not_truncated(self):
        from sqlalchemy import text

        from paw_backend.repositories import TooManyCheckoutsError, limits

        a, _ = await self.register("src/a", "a")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories (project_id, name, default_branch,"
                    " source, created_at, updated_at) SELECT :p, 'many' || n, 'main',"
                    " 'github_clone', now(), now() FROM generate_series(1, :n) AS n"
                ),
                {"p": self.project_id, "n": limits.MAX_SCOPE_CHECKOUTS},
            )
            connection.execute(
                text(
                    "INSERT INTO repository_checkouts (repository_id, project_id,"
                    " user_id, path, state, root_device, root_inode, created_at,"
                    " updated_at) SELECT id, project_id, :u, '/far/away/' || name,"
                    " 'ready', 1, 1, now(), now() FROM repositories"
                    " WHERE name LIKE 'many%'"
                ),
                {"u": self.manager},
            )

        with self.assertRaises(TooManyCheckoutsError):
            await self.scope(a)
