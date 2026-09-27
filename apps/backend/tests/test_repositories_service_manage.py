"""Managing repositories: remove, remotes, ACL, reading, purge, Tool Broker seam.

Real PostgreSQL and the real Authorizer. Skipped unless ``PAW_TEST_DATABASE_URL`` is
set.
"""

import os
import uuid

from paw_backend.authz import Authorizer, Capability, Principal, ProjectState, Resource
from paw_backend.authz.audit import AuditEvent
from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import ProjectStatus
from paw_backend.repositories import (
    CheckoutNotFoundError,
    InvalidRepositoryInputError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RemoteAlreadyRegisteredError,
    RemoteError,
    RemoteNotFoundError,
    RemoteProblem,
    RepositoryNotFoundError,
    RepositoryPermissionDeniedError,
    limits,
)
from paw_backend.tools.scope import (
    RealpathResolver,
    ScopedRepository,
    Target,
    TargetKind,
    TaskScope,
    classify_targets,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)

A = "https://github.com/acme/tool"


class ManageTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.manager = self.seed_user_with_account("alice")
        self.contributor = self.seed_user_with_account("carol")
        self.viewer = self.seed_user_with_account("vera")
        self.outsider = self.seed_user_with_account("olga")
        self.seed_manager(self.project_id, self.manager)
        self.seed_member(self.project_id, self.contributor, ProjectRole.CONTRIBUTOR)
        self.seed_member(self.project_id, self.viewer, ProjectRole.VIEWER)
        self.repository = self.seed_repository(
            self.project_id, name="tool", remotes=(A, A + ".git")
        )

    def users(self):
        return {
            "contributor": self.actor(self.contributor),
            "viewer": self.actor(self.viewer),
        }

    async def _denied(self, call):
        """``call(actor)`` is refused for a contributor, a viewer and an outsider."""
        for name, actor in self.users().items():
            with self.subTest(role=name):
                with self.assertRaises(RepositoryPermissionDeniedError) as raised:
                    await call(actor)
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        with self.subTest(role="outsider"):
            with self.assertRaises(ProjectUnavailableError):
                await call(self.actor(self.outsider))


@requires_postgres
class RemoveRepositoryTest(ManageTestCase):
    async def test_a_manager_removes_the_registration_of_everyone(self):
        self.seed_checkout(self.repository, self.project_id, self.manager, "/a/one")
        self.seed_checkout(self.repository, self.project_id, self.contributor, "/b/one")
        other = self.seed_repository(self.project_id, name="other")
        self.seed_checkout(other, self.project_id, self.manager, "/a/two")

        removed = await self.service.remove_repository(
            self.actor(self.manager), self.project_id, self.repository
        )

        self.assertEqual((removed.id, removed.name), (self.repository, "tool"))
        self.assertEqual(
            [r["name"] for r in self.repository_rows(self.project_id)], ["other"]
        )
        self.assertEqual(self.remote_urls(self.repository), [])
        self.assertEqual([c["path"] for c in self.checkout_rows()], ["/a/two"])

    async def test_no_file_and_no_remote_repository_is_deleted(self):
        home = self.account(self.manager).home
        path = f"{home}/src/tool"
        self.world.make_repository(path)
        self.seed_checkout(self.repository, self.project_id, self.manager, path)

        await self.service.remove_repository(
            self.actor(self.manager), self.project_id, self.repository
        )

        self.assertTrue(fs.isdir(f"{path}/.git"))
        self.assertEqual(fs.read(path, "code.txt"), "code\n")

    async def test_the_audit_names_the_repository(self):
        await self.service.remove_repository(
            self.actor(self.manager), self.project_id, self.repository
        )
        (event,) = self.sink.events
        self.assertIsInstance(event, AuditEvent)
        self.assertEqual(
            (event.action, event.decision, event.resource_kind, event.resource_id),
            ("project.repo.add", "allow", "repository", self.repository),
        )

    async def test_only_a_manager_can_remove(self):
        await self._denied(
            lambda actor: self.service.remove_repository(
                actor, self.project_id, self.repository
            )
        )
        self.assertEqual(len(self.repository_rows(self.project_id)), 1)

    async def test_the_project_must_be_active(self):
        for status in (ProjectStatus.ARCHIVED, ProjectStatus.PENDING_DELETION):
            project = self.seed_project(status, name="Other")
            self.seed_manager(project, self.manager)
            repository = self.seed_repository(project)
            with self.subTest(status=status):
                with self.assertRaises(ProjectNotActiveError):
                    await self.service.remove_repository(
                        self.actor(self.manager), project, repository
                    )
                self.assertEqual(len(self.repository_rows(project)), 1)

    async def test_a_repository_of_another_project_or_a_missing_one_is_not_found(self):
        other = self.seed_project(name="Other")
        self.seed_manager(other, self.manager)
        foreign = self.seed_repository(other, name="foreign")
        for project, repository in (
            (self.project_id, uuid.uuid4()),
            (self.project_id, foreign),
        ):
            with self.subTest(repository=repository == foreign):
                with self.assertRaises(RepositoryNotFoundError):
                    await self.service.remove_repository(
                        self.actor(self.manager), project, repository
                    )
        self.assertEqual(len(self.repository_rows(other)), 1)


@requires_postgres
class RemoteTest(ManageTestCase):
    async def test_a_url_is_registered_in_the_canonical_form(self):
        remote = await self.service.add_remote(
            self.actor(self.manager),
            self.project_id,
            self.repository,
            "HTTPS://API.github.com/repos/acme/tool/",
        )

        self.assertEqual(remote.url, "https://api.github.com/repos/acme/tool")
        self.assertEqual(
            (remote.repository_id, remote.project_id),
            (self.repository, self.project_id),
        )
        self.assertIn(remote.url, self.remote_urls(self.repository))
        (event,) = self.sink.events
        self.assertEqual(
            (event.action, event.decision, event.resource_id),
            ("project.repo.add", "allow", self.repository),
        )

    async def test_a_url_belongs_to_one_repository_of_a_project(self):
        other = self.seed_repository(self.project_id, name="other")
        for repository in (self.repository, other):
            with self.subTest(repository=repository == other):
                with self.assertRaises(RemoteAlreadyRegisteredError):
                    await self.service.add_remote(
                        self.actor(self.manager), self.project_id, repository, A
                    )
        self.assertEqual(self.remote_urls(other), [])

    async def test_the_same_url_may_belong_to_repositories_of_different_projects(self):
        second = self.seed_project(name="Beta")
        self.seed_manager(second, self.manager)
        repository = self.seed_repository(second, name="tool")

        remote = await self.service.add_remote(
            self.actor(self.manager), second, repository, A
        )

        self.assertEqual(remote.url, A)

    async def test_a_repository_holds_at_most_eight_urls(self):
        actor = self.actor(self.manager)
        for index in range(limits.MAX_REMOTES_PER_REPOSITORY - 2):
            await self.service.add_remote(
                actor, self.project_id, self.repository, f"{A}-{index}"
            )
        self.assertEqual(
            len(self.remote_urls(self.repository)), limits.MAX_REMOTES_PER_REPOSITORY
        )

        with self.assertRaises(RemoteError) as raised:
            await self.service.add_remote(
                actor, self.project_id, self.repository, f"{A}-more"
            )

        self.assertIs(raised.exception.problem, RemoteProblem.TOO_MANY)

    async def test_a_url_that_is_not_a_plain_https_url_is_refused_before_anything(self):
        for url in (
            "http://github.com/a/b",
            "https://u:p@github.com/a/b",
            "git@github.com:a/b",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidRepositoryInputError):
                    await self.service.add_remote(
                        self.actor(self.manager), self.project_id, self.repository, url
                    )
        self.assertEqual(self.sink.events, [])

    async def test_only_a_manager_changes_remotes(self):
        await self._denied(
            lambda actor: self.service.add_remote(
                actor, self.project_id, self.repository, "https://github.com/x/y"
            )
        )
        await self._denied(
            lambda actor: self.service.remove_remote(
                actor, self.project_id, self.repository, A
            )
        )
        self.assertEqual(len(self.remote_urls(self.repository)), 2)

    async def test_a_url_is_removed_by_its_canonical_form(self):
        await self.service.remove_remote(
            self.actor(self.manager), self.project_id, self.repository, A + "/"
        )

        self.assertEqual(self.remote_urls(self.repository), [A + ".git"])
        with self.assertRaises(RemoteNotFoundError):
            await self.service.remove_remote(
                self.actor(self.manager), self.project_id, self.repository, A
            )

    async def test_a_url_of_another_repository_cannot_be_removed_through_this_one(self):
        other = self.seed_repository(
            self.project_id, name="other", remotes=("https://github.com/x/other",)
        )
        with self.assertRaises(RemoteNotFoundError):
            await self.service.remove_remote(
                self.actor(self.manager),
                self.project_id,
                self.repository,
                "https://github.com/x/other",
            )
        self.assertEqual(self.remote_urls(other), ["https://github.com/x/other"])

    async def test_the_project_must_be_active(self):
        self.set_project(self.project_id, status="archived")
        with self.assertRaises(ProjectNotActiveError):
            await self.service.add_remote(
                self.actor(self.manager),
                self.project_id,
                self.repository,
                "https://github.com/x/y",
            )


@requires_postgres
class AclTest(ManageTestCase):
    def authorizer(self):
        return Authorizer(self.sink, clock=self.clock)

    async def decide(self, user, capability, repository):
        principal = Principal(
            user, self.actor(user).system_role, {self.project_id: await self.role(user)}
        )
        resource = Resource.repository(
            self.project_id, ProjectState.ACTIVE, repository.acl
        )
        return await self.authorizer().authorize(principal, capability, resource)

    async def role(self, user):
        return {
            self.manager: ProjectRole.MANAGER,
            self.contributor: ProjectRole.CONTRIBUTOR,
            self.viewer: ProjectRole.VIEWER,
        }[user]

    async def test_the_default_is_inherit(self):
        detail = await self.service.get_repository(
            self.actor(self.manager), self.project_id, self.repository
        )
        self.assertTrue(detail.repository.acl.inherits)
        self.assertIsNone(self.repository_rows(self.project_id)[0]["acl_allowed"])

    async def test_an_override_is_stored_sorted_and_narrows_the_project_role(self):
        updated = await self.service.set_acl(
            self.actor(self.manager),
            self.project_id,
            self.repository,
            {RepoPermission.READ, RepoPermission.AGENT},
        )

        self.assertEqual(
            updated.acl.allowed, frozenset({RepoPermission.READ, RepoPermission.AGENT})
        )
        self.assertEqual(
            self.repository_rows(self.project_id)[0]["acl_allowed"], ["agent", "read"]
        )
        # A contributor may write in the project, but this repository is now
        # read / agent only: the override narrowed the role.
        write = await self.decide(
            self.contributor, Capability.PROJECT_REPO_WRITE, updated
        )
        read = await self.decide(self.contributor, Capability.PROJECT_READ, updated)
        self.assertEqual(
            (write.allowed, write.reason), (False, Reason.REPO_ACL_FORBIDS)
        )
        self.assertTrue(read.allowed)

    async def test_an_override_never_widens_the_project_role(self):
        updated = await self.service.set_acl(
            self.actor(self.manager),
            self.project_id,
            self.repository,
            list(RepoPermission),
        )
        write = await self.decide(self.viewer, Capability.PROJECT_REPO_WRITE, updated)
        self.assertEqual(
            (write.allowed, write.reason), (False, Reason.CAPABILITY_NOT_GRANTED)
        )

    async def test_an_empty_override_denies_everything_and_none_restores_inherit(self):
        actor = self.actor(self.manager)
        updated = await self.service.set_acl(
            actor, self.project_id, self.repository, []
        )
        self.assertEqual(updated.acl.allowed, frozenset())
        self.assertEqual(self.repository_rows(self.project_id)[0]["acl_allowed"], [])
        read = await self.decide(self.viewer, Capability.PROJECT_READ, updated)
        self.assertEqual((read.allowed, read.reason), (False, Reason.REPO_ACL_FORBIDS))

        restored = await self.service.set_acl(
            actor, self.project_id, self.repository, None
        )

        self.assertTrue(restored.acl.inherits)
        self.assertIsNone(self.repository_rows(self.project_id)[0]["acl_allowed"])

    async def test_the_change_is_audited_as_a_settings_change_of_the_repository(self):
        await self.service.set_acl(
            self.actor(self.manager),
            self.project_id,
            self.repository,
            [RepoPermission.READ],
        )
        (event,) = self.sink.events
        self.assertEqual(
            (event.action, event.decision, event.resource_kind, event.resource_id),
            ("project.settings.manage", "allow", "repository", self.repository),
        )

    async def test_only_a_manager_of_an_active_project_sets_it(self):
        await self._denied(
            lambda actor: self.service.set_acl(
                actor, self.project_id, self.repository, [RepoPermission.READ]
            )
        )
        self.set_project(self.project_id, status="archived")
        with self.assertRaises(ProjectNotActiveError):
            await self.service.set_acl(
                self.actor(self.manager), self.project_id, self.repository, None
            )
        self.assertIsNone(self.repository_rows(self.project_id)[0]["acl_allowed"])

    async def test_the_permissions_must_be_permission_members(self):
        for bad in (
            "read",
            ["read"],
            [RepoPermission.READ, "write"],
            5,
            b"read",
            [None],
        ):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidRepositoryInputError):
                    await self.service.set_acl(
                        self.actor(self.manager), self.project_id, self.repository, bad
                    )
        self.assertEqual(self.sink.events, [])


@requires_postgres
class ReadTest(ManageTestCase):
    async def test_a_member_reads_the_repository_and_its_remotes(self):
        for user in (self.manager, self.contributor, self.viewer):
            with self.subTest(user=user):
                detail = await self.service.get_repository(
                    self.actor(user), self.project_id, self.repository
                )
                self.assertEqual(detail.repository.name, "tool")
                self.assertEqual(detail.remotes, (A, A + ".git"))
        # A read is audited only when it is denied.
        self.assertEqual(self.sink.events, [])

    async def test_an_outsider_and_a_missing_repository(self):
        with self.assertRaises(ProjectUnavailableError):
            await self.service.get_repository(
                self.actor(self.outsider), self.project_id, self.repository
            )
        with self.assertRaises(ProjectUnavailableError):
            await self.service.get_repository(
                self.actor(self.outsider), self.project_id, uuid.uuid4()
            )  # the same answer whether or not the repository exists
        with self.assertRaises(RepositoryNotFoundError):
            await self.service.get_repository(
                self.actor(self.viewer), self.project_id, uuid.uuid4()
            )

    async def test_an_archived_project_is_readable_and_a_pending_one_is_not(self):
        self.set_project(self.project_id, status="archived")
        detail = await self.service.get_repository(
            self.actor(self.viewer), self.project_id, self.repository
        )
        self.assertEqual(detail.repository.id, self.repository)
        pending = self.seed_project(ProjectStatus.PENDING_DELETION, name="Gone")
        self.seed_member(pending, self.viewer, ProjectRole.VIEWER)
        repository = self.seed_repository(pending)
        with self.assertRaises(ProjectNotActiveError):
            await self.service.get_repository(
                self.actor(self.viewer), pending, repository
            )

    async def test_a_repository_whose_acl_denies_read_is_hidden_from_members(self):
        hidden = self.seed_repository(
            self.project_id, name="hidden", acl_allowed=["write"]
        )
        with self.assertRaises(RepositoryNotFoundError):
            await self.service.get_repository(
                self.actor(self.viewer), self.project_id, hidden
            )
        listed = await self.service.list_repositories(
            self.actor(self.viewer), self.project_id
        )
        self.assertEqual([r.name for r in listed], ["tool"])

    async def test_an_override_that_keeps_read_still_lists_the_repository(self):
        for name, acl in (
            ("read-only", ["read"]),
            ("everything", ["agent", "read", "write"]),
            ("no-read", ["agent", "write"]),
            ("nothing", []),
        ):
            self.seed_repository(self.project_id, name=name, acl_allowed=acl)

        listed = await self.service.list_repositories(
            self.actor(self.viewer), self.project_id
        )

        self.assertEqual([r.name for r in listed], ["everything", "read-only", "tool"])

    async def test_the_list_is_sorted_by_name_without_case_and_paged(self):
        for name in ("Zeta", "alpha", "Beta"):
            self.seed_repository(self.project_id, name=name)
        actor = self.actor(self.viewer)

        everything = await self.service.list_repositories(actor, self.project_id)
        self.assertEqual(
            [r.name for r in everything], ["alpha", "Beta", "tool", "Zeta"]
        )
        page = await self.service.list_repositories(
            actor, self.project_id, limit=2, offset=1
        )
        self.assertEqual([r.name for r in page], ["Beta", "tool"])
        self.assertEqual(
            await self.service.list_repositories(actor, self.project_id, offset=10), ()
        )

    async def test_a_page_is_full_even_when_hidden_repositories_are_in_the_way(self):
        for name in ("a1", "a2", "a3"):
            self.seed_repository(self.project_id, name=name, acl_allowed=[])
        page = await self.service.list_repositories(
            self.actor(self.viewer), self.project_id, limit=1
        )
        self.assertEqual([r.name for r in page], ["tool"])

    async def test_an_outsider_cannot_list(self):
        with self.assertRaises(ProjectUnavailableError):
            await self.service.list_repositories(
                self.actor(self.outsider), self.project_id
            )
        self.assertEqual(self.audit_actions(), [("project.read", "deny", "project")])


@requires_postgres
class PurgeTest(ManageTestCase):
    async def test_only_the_registrations_of_deleted_projects_go(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        gone = self.seed_repository(
            deleted, name="gone", remotes=("https://github.com/x/gone",)
        )
        self.seed_checkout(gone, deleted, self.manager, "/a/gone")
        archived = self.seed_project(ProjectStatus.ARCHIVED, name="Arch")
        kept = self.seed_repository(archived, name="kept")

        purged = await self.service.purge_projects([deleted, archived, self.project_id])

        self.assertEqual(purged, (deleted,))
        self.assertEqual(self.repository_rows(deleted), [])
        self.assertEqual(self.remote_urls(gone), [])
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual([r["id"] for r in self.repository_rows(archived)], [kept])
        self.assertEqual(len(self.repository_rows(self.project_id)), 1)

    async def test_files_are_not_touched(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        repository = self.seed_repository(deleted, name="gone")
        home = self.account(self.manager).home
        path = f"{home}/src/gone"
        self.world.make_repository(path)
        self.seed_checkout(repository, deleted, self.manager, path)

        await self.service.purge_projects([deleted])

        self.assertTrue(fs.isdir(f"{path}/.git"))

    async def test_nothing_to_purge_is_fine_and_bad_input_is_refused(self):
        self.assertEqual(await self.service.purge_projects([]), ())
        self.assertEqual(await self.service.purge_projects([uuid.uuid4()]), ())
        for bad in ("x", None, ["x"], [1]):
            with self.assertRaises(InvalidRepositoryInputError):
                await self.service.purge_projects(bad)


@requires_postgres
@requires_git
class ScopeEntriesTest(ManageTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.home = self.account(self.manager).home

    def checkout_at(self, repository, project, path, user=None):
        os.makedirs(path, exist_ok=True)
        return self.seed_checkout(repository, project, user or self.manager, path)

    async def entries(self, repository=None, project=None, user=None):
        return await self.service.scope_entries(
            user or self.manager,
            project or self.project_id,
            repository or self.repository,
        )

    async def test_the_entry_carries_root_acl_and_remotes_for_the_tool_broker(self):
        path = f"{self.home}/src/tool"
        self.checkout_at(self.repository, self.project_id, path)

        (entry,) = await self.entries()

        self.assertEqual(
            entry,
            ScopedRepository(
                repo_id=self.repository,
                project_id=self.project_id,
                root=path,
                acl=entry.acl,
                remotes=(A, A + ".git"),
            ),
        )
        self.assertTrue(entry.acl.inherits)
        self.assertEqual(
            (entry.acl.repo_id, entry.acl.project_id),
            (self.repository, self.project_id),
        )

    async def test_the_stored_override_is_the_resolved_acl(self):
        self.checkout_at(self.repository, self.project_id, f"{self.home}/src/tool")
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(
                text("UPDATE repositories SET acl_allowed = ARRAY['read']")
            )
        (entry,) = await self.entries()
        self.assertEqual(entry.acl.allowed, frozenset({RepoPermission.READ}))

    async def test_a_pending_or_missing_checkout_is_not_usable(self):
        with self.assertRaises(CheckoutNotFoundError):
            await self.entries()
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.manager,
            f"{self.home}/p",
            state="pending",
        )
        with self.assertRaises(CheckoutNotFoundError):
            await self.entries()
        with self.assertRaises(RepositoryNotFoundError):
            await self.entries(repository=uuid.uuid4())
        with self.assertRaises(RepositoryNotFoundError):
            await self.entries(project=uuid.uuid4())

    async def test_only_the_users_own_checkout_is_used(self):
        self.checkout_at(self.repository, self.project_id, f"{self.home}/src/tool")
        with self.assertRaises(CheckoutNotFoundError):
            await self.entries(user=self.contributor)

    async def test_nested_repositories_bring_both_acls_in_both_directions(self):
        second = self.seed_project(name="Second")
        inner_repo = self.seed_repository(
            second,
            name="inner",
            acl_allowed=["read"],
            remotes=("https://github.com/acme/inner",),
        )
        outer = f"{self.home}/src/outer"
        inner = f"{outer}/vendor/inner"
        self.checkout_at(self.repository, self.project_id, outer)
        self.checkout_at(inner_repo, second, inner)
        # Neighbours that are not nested: a sibling, a longer name with the same
        # prefix, and a checkout of another user.
        for name in ("outer2", "outer-evil"):
            extra = self.seed_repository(self.project_id, name=name)
            self.checkout_at(extra, self.project_id, f"{self.home}/src/{name}")
        elsewhere = self.seed_repository(self.project_id, name="els")
        self.checkout_at(elsewhere, self.project_id, f"{self.home}/other/els")
        bob = self.seed_repository(self.project_id, name="bobs")
        self.checkout_at(
            bob,
            self.project_id,
            f"{self.account(self.contributor).home}/outer/x",
            self.contributor,
        )

        from_outer = await self.entries()
        from_inner = await self.entries(repository=inner_repo, project=second)

        self.assertEqual([e.repo_id for e in from_outer], [self.repository, inner_repo])
        self.assertEqual([e.repo_id for e in from_inner], [inner_repo, self.repository])
        by_id = {e.repo_id: e for e in from_outer}
        self.assertEqual(by_id[inner_repo].project_id, second)  # its own project
        self.assertEqual(
            by_id[inner_repo].acl.allowed, frozenset({RepoPermission.READ})
        )
        self.assertTrue(by_id[self.repository].acl.inherits)
        self.assertEqual(by_id[inner_repo].remotes, ("https://github.com/acme/inner",))

    async def test_a_pending_nested_checkout_is_not_part_of_the_scope(self):
        outer = f"{self.home}/src/outer"
        self.checkout_at(self.repository, self.project_id, outer)
        nested = self.seed_repository(self.project_id, name="nested")
        os.makedirs(f"{outer}/n")
        self.seed_checkout(
            nested, self.project_id, self.manager, f"{outer}/n", state="pending"
        )
        self.assertEqual([e.repo_id for e in await self.entries()], [self.repository])

    async def test_wildcard_characters_in_a_path_do_not_make_repositories_nested(self):
        # LIKE would read ``_`` as "any character": ``a_b/%`` would match ``aXb/...``.
        first = self.seed_repository(self.project_id, name="under")
        second = self.seed_repository(self.project_id, name="lookalike")
        self.checkout_at(first, self.project_id, f"{self.home}/src/a_b")
        self.checkout_at(second, self.project_id, f"{self.home}/src/aXb/inner")
        percent = self.seed_repository(self.project_id, name="percent")
        self.checkout_at(percent, self.project_id, f"{self.home}/src/100%")
        other = self.seed_repository(self.project_id, name="other")
        self.checkout_at(other, self.project_id, f"{self.home}/src/100x/inner")

        self.assertEqual(
            [e.repo_id for e in await self.entries(repository=first)], [first]
        )
        self.assertEqual(
            [e.repo_id for e in await self.entries(repository=percent)], [percent]
        )

    async def test_the_tool_broker_maps_a_path_in_the_inner_repository_to_both(self):
        second = self.seed_project(name="Second")
        inner_repo = self.seed_repository(second, name="inner")
        outer = f"{self.home}/src/outer"
        inner = f"{outer}/vendor/inner"
        self.checkout_at(self.repository, self.project_id, outer)
        self.checkout_at(inner_repo, second, inner)
        entries = await self.entries()
        scope = TaskScope(
            path_roots=(self.home,),
            hosts=frozenset({"github.com"}),
            projects={
                self.project_id: ProjectState.ACTIVE,
                second: ProjectState.ACTIVE,
            },
            repositories=entries,
        )
        resolver = RealpathResolver()

        inside_inner = await classify_targets(
            [Target(TargetKind.PATH, f"{inner}/file.txt")], scope, resolver
        )
        inside_outer = await classify_targets(
            [Target(TargetKind.PATH, f"{outer}/README.md")], scope, resolver
        )
        by_url = await classify_targets(
            [Target(TargetKind.HOST, "github.com")],
            scope,
            resolver,
            urls=[A + "/info/refs"],
        )

        self.assertEqual(set(inside_inner.repositories), {self.repository, inner_repo})
        self.assertEqual(inside_outer.repositories, (self.repository,))
        self.assertEqual(by_url.repositories, (self.repository,))
