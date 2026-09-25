"""Two processes at once: the database and the row locks have the last word.

Each service runs on an engine of its own (as a second backend process would). Real
PostgreSQL, real git on local temporary repositories. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import os

from paw_backend.authz.roles import ProjectRole
from paw_backend.repositories import (
    CheckoutExistsError,
    CheckoutInProgressError,
    ProjectNotActiveError,
    RemoteAlreadyRegisteredError,
    RepositoryBusyError,
    RepositoryLimitError,
    RepositoryNameTakenError,
    limits,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)


class ConcurrentTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.manager = self.seed_user_with_account("alice")
        self.member = self.seed_user_with_account("carol")
        self.seed_manager(self.project_id, self.manager)
        self.seed_member(self.project_id, self.member, ProjectRole.CONTRIBUTOR)
        self.world.make_bare("acme", "tool")
        self.world.make_bare("acme", "other")

    def outcomes(self, results):
        """``(successes, sorted error type names)`` of a ``gather`` of tasks."""
        errors = [r for r in results if isinstance(r, BaseException)]
        return len(results) - len(errors), sorted(type(e).__name__ for e in errors)


@requires_postgres
@requires_git
class ConcurrentRegistrationTest(ConcurrentTestCase):
    async def test_two_registrations_of_one_name_leave_exactly_one(self):
        first, second = self.new_service(), self.new_service()

        results = await asyncio.gather(
            first.clone_from_github(
                self.actor(self.manager), self.project_id, "acme/tool", name="Same"
            ),
            second.clone_from_github(
                self.actor(self.manager), self.project_id, "acme/other", name="same"
            ),
            return_exceptions=True,
        )

        self.assertEqual(
            self.outcomes(results), (1, [RepositoryNameTakenError.__name__])
        )
        (row,) = self.repository_rows(self.project_id)
        (checkout,) = self.checkout_rows()
        self.assertEqual(checkout["state"], "ready")
        self.assertTrue(fs.isdir(checkout["path"]))
        # The loser's directory is gone: only one project directory entry remains.
        parent = os.path.dirname(checkout["path"])
        self.assertEqual(len(fs.listdir(parent)), 1)
        self.assertIn(row["name"], ("Same", "same"))

    async def test_two_users_registering_one_url_leave_exactly_one_repository(self):
        second_user = self.seed_user_with_account("dora")
        self.seed_manager(self.project_id, second_user)
        one, two = self.new_service(), self.new_service()

        results = await asyncio.gather(
            one.clone_from_github(
                self.actor(self.manager), self.project_id, "acme/tool", name="one"
            ),
            two.clone_from_github(
                self.actor(second_user), self.project_id, "acme/tool", name="two"
            ),
            return_exceptions=True,
        )

        self.assertEqual(
            self.outcomes(results), (1, [RemoteAlreadyRegisteredError.__name__])
        )
        self.assertEqual(len(self.repository_rows(self.project_id)), 1)
        self.assertEqual(len(self.checkout_rows()), 1)

    async def test_the_repository_limit_holds_under_concurrency(self):
        self.seed_many_repositories(
            self.project_id, limits.MAX_REPOSITORIES_PER_PROJECT - 1
        )
        services = [self.new_service() for _ in range(3)]

        results = await asyncio.gather(
            *(
                service.create_local(
                    self.actor(self.manager), self.project_id, f"new{index}"
                )
                for index, service in enumerate(services)
            ),
            return_exceptions=True,
        )

        self.assertEqual(
            self.outcomes(results), (1, [RepositoryLimitError.__name__] * 2)
        )
        self.assertEqual(
            len(self.repository_rows(self.project_id)),
            limits.MAX_REPOSITORIES_PER_PROJECT,
        )

    async def test_two_registrations_of_one_directory_leave_exactly_one(self):
        other = self.seed_project(name="Beta")
        self.seed_manager(other, self.manager)
        path = f"{self.account(self.manager).home}/src/tool"
        os.makedirs(os.path.dirname(path))
        self.world.make_repository(path)
        one, two = self.new_service(), self.new_service()

        results = await asyncio.gather(
            one.register_existing(self.actor(self.manager), self.project_id, path),
            two.register_existing(self.actor(self.manager), other, path),
            return_exceptions=True,
        )

        self.assertEqual(self.outcomes(results), (1, [CheckoutExistsError.__name__]))
        self.assertEqual(len(self.checkout_rows()), 1)
        self.assertEqual(
            len(self.repository_rows(self.project_id))
            + len(self.repository_rows(other)),
            1,
            "the loser left no repository row behind",
        )

    async def test_two_urls_added_to_two_repositories_go_to_one(self):
        first = self.seed_repository(self.project_id, name="first")
        second = self.seed_repository(self.project_id, name="second")
        url = "https://github.com/acme/shared"
        one, two = self.new_service(), self.new_service()

        results = await asyncio.gather(
            one.add_remote(self.actor(self.manager), self.project_id, first, url),
            two.add_remote(self.actor(self.manager), self.project_id, second, url),
            return_exceptions=True,
        )

        self.assertEqual(
            self.outcomes(results), (1, [RemoteAlreadyRegisteredError.__name__])
        )
        owners = [r for r in (first, second) if self.remote_urls(r) == [url]]
        self.assertEqual(len(owners), 1)


@requires_postgres
@requires_git
class ConcurrentCheckoutTest(ConcurrentTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.repository = self.seed_repository(
            self.project_id,
            name="tool",
            remotes=("https://github.com/acme/tool.git",),
        )

    async def test_one_user_asking_twice_gets_one_checkout(self):
        one, two = self.new_service(), self.new_service()

        results = await asyncio.gather(
            one.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            ),
            two.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            ),
            return_exceptions=True,
        )

        successes, errors = self.outcomes(results)
        self.assertEqual(successes, 1)
        self.assertIn(
            errors,
            (
                [CheckoutExistsError.__name__],
                [CheckoutInProgressError.__name__],
            ),
        )
        (row,) = self.checkout_rows(self.repository)
        self.assertEqual(row["state"], "ready")
        self.assertTrue(fs.isdir(f"{row['path']}/.git"))

    async def test_two_users_each_get_their_own(self):
        one, two = self.new_service(), self.new_service()

        results = await asyncio.gather(
            one.create_checkout(
                self.actor(self.manager), self.project_id, self.repository
            ),
            two.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            ),
        )

        self.assertEqual(len({r.path for r in results}), 2)
        self.assertEqual(
            {row["user_id"] for row in self.checkout_rows(self.repository)},
            {self.manager, self.member},
        )


@requires_postgres
@requires_git
class RowLockTest(ConcurrentTestCase):
    async def test_a_registration_waits_for_the_project_and_then_sees_it_archived(self):
        connection, transaction = self.lock_project(self.project_id)
        task = self.spawn(
            self.new_service().create_local(
                self.actor(self.manager), self.project_id, "fresh"
            )
        )
        await asyncio.sleep(0.5)
        self.assertWaiting(task)

        connection.exec_driver_sql(
            "UPDATE projects SET status = 'archived' WHERE id = %s", (self.project_id,)
        )
        transaction.commit()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertEqual(self.repository_rows(self.project_id), [])
        self.assertFalse(fs.lexists(f"{self.account(self.manager).home}/workspaces"))

    async def test_registering_an_existing_repository_waits_and_sees_the_archive(self):
        path = f"{self.account(self.manager).home}/src/tool"
        os.makedirs(os.path.dirname(path))
        self.world.make_repository(path)
        connection, transaction = self.lock_project(self.project_id)
        task = self.spawn(
            self.new_service().register_existing(
                self.actor(self.manager), self.project_id, path
            )
        )
        await asyncio.sleep(0.5)
        self.assertWaiting(task)

        connection.exec_driver_sql(
            "UPDATE projects SET status = 'archived' WHERE id = %s", (self.project_id,)
        )
        transaction.commit()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertEqual(self.repository_rows(self.project_id), [])
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_lock_that_is_held_too_long_is_a_busy_error_and_changes_nothing(
        self,
    ):
        self.lock_project(self.project_id)
        service = self.new_service(lock_timeout_ms=100)

        with self.assertRaises(RepositoryBusyError):
            await service.create_local(
                self.actor(self.manager), self.project_id, "fresh"
            )

        self.assertEqual(self.repository_rows(self.project_id), [])
        self.assertFalse(fs.lexists(f"{self.account(self.manager).home}/workspaces"))

    async def test_a_checkout_waits_for_a_writer_of_the_project(self):
        repository = self.seed_repository(
            self.project_id, name="tool", remotes=("https://github.com/acme/tool.git",)
        )
        connection, transaction = self.lock_project(self.project_id)
        task = self.spawn(
            self.new_service().create_checkout(
                self.actor(self.member), self.project_id, repository
            )
        )
        await asyncio.sleep(0.5)
        self.assertWaiting(task)

        transaction.commit()
        connection.close()

        checkout = await task
        self.assertEqual(checkout.repository_id, repository)
