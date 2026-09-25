"""Concurrent use of ``ProjectService`` (real PostgreSQL).

Every operation that changes a project starts by locking the project row, so
all changes to one project are serialised: that is what makes "the last Manager
cannot leave" true when two Managers leave at the same time. No test depends on
machine speed: a waiting operation is held back by a row lock the test itself
holds, and every wait for the outcome has a generous deadline.
"""

import asyncio
import unittest
from datetime import timedelta

from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import (
    LastManagerError,
    MemberStatus,
    ProjectBusyError,
    ProjectNotFoundError,
    ProjectStatus,
)

from .projects_support import T0, requires_postgres
from .test_projects_service_access import AccessTestCase

MANAGER, VIEWER = ProjectRole.MANAGER, ProjectRole.VIEWER
DEADLINE = 30


@requires_postgres
class ConcurrencyTest(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.first = self.seed_manager(self.project_id)
        self.second = self.seed_manager(self.project_id)

    async def test_two_managers_leaving_at_once_leave_exactly_one_manager(self):
        _, transaction = self.lock_project(self.project_id)
        services = [self.new_service(), self.new_service()]
        tasks = [
            self.spawn(service.leave_project(self.actor(user), self.project_id))
            for service, user in zip(services, (self.first, self.second), strict=True)
        ]
        await asyncio.sleep(0.3)
        for task in tasks:
            self.assertWaiting(task)

        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if r is None]
        failures = [r for r in results if isinstance(r, LastManagerError)]
        self.assertEqual((len(successes), len(failures)), (1, 1), results)
        remaining = [
            row
            for row in self.member_rows(self.project_id).values()
            if row["role"] == "manager"
        ]
        self.assertEqual(len(remaining), 1)

    async def test_a_manager_removing_the_other_while_being_removed_keeps_one(self):
        _, transaction = self.lock_project(self.project_id)
        services = [self.new_service(), self.new_service()]
        tasks = [
            self.spawn(
                services[0].remove_member(
                    self.actor(self.first), self.project_id, self.second
                )
            ),
            self.spawn(
                services[1].remove_member(
                    self.actor(self.second), self.project_id, self.first
                )
            ),
        ]
        await asyncio.sleep(0.3)
        for task in tasks:
            self.assertWaiting(task)
        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # The one that ran second was already removed (not found) or would have
        # left no Manager (refused): never both succeed.
        self.assertEqual(sum(r is None for r in results), 1, results)
        self.assertEqual(
            sum(
                isinstance(r, ProjectNotFoundError | LastManagerError) for r in results
            ),
            1,
            results,
        )
        managers = [
            r
            for r in self.member_rows(self.project_id).values()
            if r["role"] == "manager"
        ]
        self.assertEqual(len(managers), 1)

    async def test_a_demotion_and_a_departure_cannot_both_succeed(self):
        _, transaction = self.lock_project(self.project_id)
        a, b = self.new_service(), self.new_service()
        tasks = [
            self.spawn(
                a.change_role(
                    self.actor(self.first), self.project_id, self.second, VIEWER
                )
            ),
            self.spawn(b.leave_project(self.actor(self.first), self.project_id)),
        ]
        await asyncio.sleep(0.3)
        for task in tasks:
            self.assertWaiting(task)
        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            results = await asyncio.gather(*tasks, return_exceptions=True)

        managers = [
            r
            for r in self.member_rows(self.project_id).values()
            if r["role"] == "manager"
        ]
        self.assertGreaterEqual(len(managers), 1, results)

    async def test_a_changing_operation_waits_for_the_project_row_lock(self):
        _, transaction = self.lock_project(self.project_id)
        task = self.spawn(
            self.service.rename_project(self.actor(self.first), self.project_id, "New")
        )
        await asyncio.sleep(0.3)
        self.assertWaiting(task)
        self.assertEqual(self.project_row(self.project_id)["name"], "Alpha")

        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            project = await task
        self.assertEqual(project.name, "New")

    async def test_every_changing_operation_gives_up_after_the_lock_timeout(self):
        viewer = self.seed_member(self.project_id, role=VIEWER)
        invitee = self.seed_user()
        pending_invitee = self.seed_member(self.project_id, status=MemberStatus.INVITED)
        service = self.new_service(lock_timeout_ms=200)
        manager = self.actor(self.first)
        _, transaction = self.lock_project(self.project_id)
        before = self.snapshot()

        calls = {
            "rename_project": service.rename_project(manager, self.project_id, "X"),
            "set_description": service.set_description(manager, self.project_id, "x"),
            "invite_member": service.invite_member(
                manager, self.project_id, invitee, VIEWER
            ),
            "remove_member": service.remove_member(manager, self.project_id, viewer),
            "change_role": service.change_role(
                manager, self.project_id, viewer, MANAGER
            ),
            "leave_project": service.leave_project(self.actor(viewer), self.project_id),
            "accept_invite": service.accept_invite(
                self.actor(pending_invitee), self.project_id
            ),
            "decline_invite": service.decline_invite(
                self.actor(pending_invitee), self.project_id
            ),
            "archive": service.archive(manager, self.project_id),
            "unarchive": service.unarchive(manager, self.project_id),
            "begin_deletion": service.begin_deletion(manager, self.project_id, "Alpha"),
            "restore": service.restore(manager, self.project_id),
        }
        for name, awaitable in calls.items():
            with self.subTest(method=name):
                async with asyncio.timeout(DEADLINE):
                    with self.assertRaises(ProjectBusyError) as caught:
                        await awaitable
                self.assertEqual(str(caught.exception), "The project is busy")
        transaction.rollback()
        self.assertEqual(self.snapshot(), before)

    async def test_reads_do_not_wait_for_a_row_lock(self):
        _, _ = self.lock_project(self.project_id)
        manager = self.actor(self.first)
        async with asyncio.timeout(DEADLINE):
            project = await self.service.get_project(manager, self.project_id)
            members = await self.service.list_members(manager, self.project_id)
            invites = await self.service.list_invites(manager, self.project_id)
            listed = await self.service.list_projects(manager)
            roles = await self.service.roles_of(self.first)
            mine = await self.service.list_my_invites(manager)
        self.assertEqual(project.id, self.project_id)
        self.assertEqual(len(members), 2)
        self.assertEqual((invites, mine), ((), ()))
        self.assertEqual([p.id for p in listed], [self.project_id])
        self.assertEqual(dict(roles), {self.project_id: MANAGER})

    async def test_the_lock_timeout_does_not_leak_to_the_next_transaction(self):
        service = self.new_service(lock_timeout_ms=200)
        _, transaction = self.lock_project(self.project_id)
        with self.assertRaises(ProjectBusyError):
            await service.rename_project(self.actor(self.first), self.project_id, "X")
        transaction.rollback()
        project = await service.rename_project(
            self.actor(self.first), self.project_id, "Now it works"
        )
        self.assertEqual(project.name, "Now it works")

    async def test_purge_and_restore_do_not_both_win(self):
        self.set_project(
            self.project_id,
            status="pending_deletion",
            deletion_started_at=T0 - timedelta(days=30),
            deletion_scheduled_at=T0,
        )
        _, transaction = self.lock_project(self.project_id)
        # At exactly the deadline the window is closed for a restore and the
        # purge is due; whichever gets the lock first, the outcome is one of them.
        self.clock.now = T0 - timedelta(microseconds=1)
        restore = self.spawn(
            self.new_service().restore(self.actor(self.first), self.project_id)
        )
        await asyncio.sleep(0.3)
        self.assertWaiting(restore)
        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            restored = await restore
        self.assertIs(restored.status, ProjectStatus.ARCHIVED)
        self.clock.now = T0 + timedelta(days=1)
        result = await self.service.purge_expired()
        self.assertEqual(result.purged, ())
        self.assertEqual(self.project_row(self.project_id)["status"], "archived")


if __name__ == "__main__":
    unittest.main()
