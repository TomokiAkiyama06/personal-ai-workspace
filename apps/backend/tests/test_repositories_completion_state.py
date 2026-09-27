"""A checkout is completed only while its project is Active (PAW-027).

A clone or a GitHub creation can run for minutes between the reservation (a
committed ``pending`` row) and the completion (a second transaction). Repository
changes are allowed only in an Active project, so the completion transaction locks
the project ``FOR SHARE`` and checks its state again, in every path that completes a
pending checkout: ``create_checkout``, ``clone_from_github``, ``create_local`` and
``create_github``. The tests hold the git command (or the gateway) at a barrier,
change the project with the real ``ProjectService`` (or with a held row lock), and
let the operation go on.

What the refused call does with its work follows the ownership rule of
``RepositoryService._abandon``: while its own pending row exists it undoes what it
made (row, directory, a repository nobody else has a checkout of); when a concurrent
unregistration took the row, nothing is deleted. Real PostgreSQL and real git on
local temporary repositories; skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio

from paw_backend.authz import Authorizer
from paw_backend.projects import ProjectService
from paw_backend.repositories import (
    ProjectNotActiveError,
    ProjectUnavailableError,
)

from .repositories_support import fs, requires_git, requires_postgres
from .test_repositories_unregister_race import (
    Gate,
    HeldGateway,
    RaceTestCase,
)


class StateChangeTestCase(RaceTestCase):
    def projects(self) -> ProjectService:
        database = self.service_database()
        self.addAsyncCleanup(database.dispose)
        return ProjectService(
            database, Authorizer(self.sink, clock=self.clock), clock=self.clock
        )

    async def archive(self) -> None:
        await self.projects().archive(self.alice, self.project_id)

    async def begin_deletion(self) -> None:
        await self.projects().begin_deletion(
            self.alice, self.project_id, "Alpha Project"
        )

    def assertCleanedUp(self, path: str) -> None:
        """Nothing of the refused call is left: no row, no directory, no remotes."""
        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(path), "the call's own directory was removed")

    def statuses(self):
        return {"archive": self.archive, "begin_deletion": self.begin_deletion}


@requires_postgres
@requires_git
class CreateCheckoutCompletionTest(StateChangeTestCase):
    async def start(self, gate: Gate):
        service = self.new_service(runner=gate.runner(**self.world.runner_options()))
        task = self.spawn(
            service.create_checkout(self.carol, self.project_id, self.seeded)
        )
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()
        return task, pending["path"]

    async def test_a_project_archived_or_in_deletion_refuses_the_completion(self):
        for name, change in (
            ("archived", self.archive),
            ("pending_deletion", self.begin_deletion),
        ):
            with self.subTest(change=name):
                self.set_project(self.project_id, status="active")
                gate = Gate("clone")
                task, path = await self.start(gate)

                await change()
                gate.release.set()

                with self.assertRaises(ProjectNotActiveError) as raised:
                    await task
                self.assertEqual(raised.exception.status.value, name)
                self.assertCleanedUp(path)
                self.clean_after_subtest()

    def clean_after_subtest(self) -> None:
        # The next sub-case starts from an Active project again.
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(
                text(
                    "UPDATE projects SET status = 'active', deletion_started_at = NULL,"
                    " deletion_scheduled_at = NULL WHERE id = :p"
                ),
                {"p": self.project_id},
            )

    async def test_the_completion_waits_for_a_state_change_that_is_being_made(self):
        gate = Gate("clone")
        task, path = await self.start(gate)
        connection, transaction = self.lock_project(self.project_id)
        connection.exec_driver_sql(
            "UPDATE projects SET status = 'archived' WHERE id = %s", (self.project_id,)
        )

        gate.release.set()
        await asyncio.sleep(0.5)
        self.assertWaiting(task)  # the completion asks the project first
        transaction.commit()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertCleanedUp(path)

    async def test_an_active_project_completes_as_before(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        gate.release.set()

        checkout = await task
        self.assertEqual(checkout.path, path)
        self.assertTrue(fs.isdir(f"{path}/.git"))

    async def test_a_deleted_project_is_not_found_and_nothing_is_kept(self):
        gate = Gate("clone")
        task, path = await self.start(gate)
        self.tombstone()
        gate.release.set()

        with self.assertRaises(ProjectUnavailableError):
            await task
        self.assertCleanedUp(path)

    def tombstone(self) -> None:
        from datetime import timedelta

        started = self.clock.now
        self.set_project(
            self.project_id,
            status="deleted",
            name="Deleted Project",
            description=None,
            deletion_started_at=started,
            deletion_scheduled_at=started + timedelta(hours=720),
            deleted_at=started + timedelta(hours=720),
        )

    async def test_a_state_change_and_an_unregistration_together_delete_nothing(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        await self.new_service().remove_checkout(
            self.carol, self.project_id, self.seeded
        )
        self.add_work(path)
        await self.archive()
        gate.release.set()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertDirectoryKept(path)


@requires_postgres
@requires_git
class ManagedRouteCompletionTest(StateChangeTestCase):
    async def test_clone_from_github_refuses_the_completion_and_undoes_its_work(self):
        for name, change in (
            ("archived", self.archive),
            ("pending_deletion", self.begin_deletion),
        ):
            with self.subTest(change=name):
                gate = Gate("clone")
                service = self.new_service(
                    runner=gate.runner(**self.world.runner_options())
                )
                task = self.spawn(
                    service.clone_from_github(
                        self.alice, self.project_id, "acme/tool", name=f"tool-{name}"
                    )
                )
                await self.wait_for(gate.started)
                (pending,) = [
                    c for c in self.checkout_rows() if c["state"] == "pending"
                ]

                await change()
                gate.release.set()

                with self.assertRaises(ProjectNotActiveError) as raised:
                    await task
                self.assertEqual(raised.exception.status.value, name)
                self.assertFalse(fs.lexists(pending["path"]))
                self.assertEqual(
                    [r["name"] for r in self.repository_rows(self.project_id)],
                    ["seeded"],
                    "the repository this call registered is undone",
                )
                self.set_project(
                    self.project_id,
                    status="active",
                    deletion_started_at=None,
                    deletion_scheduled_at=None,
                )

    async def test_create_local_refuses_the_completion(self):
        gate = Gate("init")
        service = self.new_service(runner=gate.runner(**self.world.runner_options()))
        task = self.spawn(service.create_local(self.alice, self.project_id, "fresh"))
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()

        await self.archive()
        gate.release.set()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertFalse(fs.lexists(pending["path"]))
        self.assertEqual(
            [r["name"] for r in self.repository_rows(self.project_id)], ["seeded"]
        )

    async def test_create_github_stores_no_remote_in_a_project_that_is_not_active(
        self,
    ):
        for name, change in (
            ("archived", self.archive),
            ("pending_deletion", self.begin_deletion),
        ):
            with self.subTest(change=name):
                gateway = HeldGateway()
                service = self.new_service(github=gateway)
                task = self.spawn(
                    service.create_github(self.alice, self.project_id, f"shared-{name}")
                )
                await self.wait_for(gateway.started)
                (pending,) = [
                    c for c in self.checkout_rows() if c["state"] == "pending"
                ]

                await change()
                gateway.release.set()

                with self.assertLogs("paw_backend.repositories.service", "ERROR"):
                    with self.assertRaises(ProjectNotActiveError):
                        await task
                self.assertFalse(fs.lexists(pending["path"]))
                self.assertEqual(
                    self.rows(
                        "SELECT url FROM repository_remotes WHERE url LIKE :u",
                        u="%alice-gh%",
                    ),
                    [],
                )
                self.assertEqual(
                    [r["name"] for r in self.repository_rows(self.project_id)],
                    ["seeded"],
                )
                self.set_project(
                    self.project_id,
                    status="active",
                    deletion_started_at=None,
                    deletion_scheduled_at=None,
                )

    async def test_the_completion_of_a_new_repository_waits_for_the_project(self):
        gate = Gate("init")
        service = self.new_service(runner=gate.runner(**self.world.runner_options()))
        task = self.spawn(service.create_local(self.alice, self.project_id, "fresh"))
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()
        connection, transaction = self.lock_project(self.project_id)
        connection.exec_driver_sql(
            "UPDATE projects SET status = 'archived' WHERE id = %s", (self.project_id,)
        )

        gate.release.set()
        await asyncio.sleep(0.5)
        self.assertWaiting(task)
        transaction.commit()

        with self.assertRaises(ProjectNotActiveError):
            await task
        self.assertFalse(fs.lexists(pending["path"]))
