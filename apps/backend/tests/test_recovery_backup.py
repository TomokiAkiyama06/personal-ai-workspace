"""The Recovery Repository checkout, git and one backup run (PAW-047, Decision 0054).

No database: the snapshot, the projection's status and the outcome recorder are
fakes. git runs for real, as the current user, on a bare repository and a clone
below a temporary directory (``RecoveryWorld``): the backup claims only an empty
clone, commits only what changed (one commit per run), pushes fast-forward to
the configured upstream only, never runs a hook, retries an earlier failed push,
and never writes through a link or outside its managed names.
"""

import asyncio
import os
import stat
import threading
import unittest
from dataclasses import replace
from unittest import mock
from uuid import uuid4

from paw_backend.db import Database
from paw_backend.memory.projection.records import ProjectionStatus
from paw_backend.memory.projection.writer import INCOMPLETE_NAME
from paw_backend.recovery import (
    BackupStep,
    RecoveryBackupRunner,
    RecoveryBusyError,
    RecoveryRestorer,
)
from paw_backend.recovery import restore as restore_module
from paw_backend.recovery.files import (
    CheckoutProblem,
    RecoveryFilesError,
    open_checkout,
    open_projection,
)
from paw_backend.recovery.format import (
    MARKER_CONTENT,
    MARKER_NAME,
)
from paw_backend.recovery.git import RecoveryGit
from paw_backend.recovery.restore import load_source

from .recovery_support import (
    BRANCH,
    T0,
    Recorder,
    RecoveryWorld,
    StaticSource,
    completed_projection,
    git,
    small_snapshot,
    snapshot_with,
    tree,
    user,
)
from .support import make_settings


class BackupTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.world = RecoveryWorld(self)
        self.memory_id = uuid4()
        self.owner_id = uuid4()
        self.world.write_projection(
            {
                f"users/{self.owner_id}/{self.memory_id}.md": b"# note\n",
                f"users/{self.owner_id}/INDEX.md": b"# index\n",
                "shared/INDEX.md": b"# shared\n",
            }
        )
        self.source = StaticSource(small_snapshot())
        self.recorder = Recorder()
        self.status = completed_projection()
        self.database = Database(make_settings())

    async def status_now(self) -> ProjectionStatus:
        return self.status

    def runner(self, **options) -> RecoveryBackupRunner:
        values = {
            "protected_homes": self.world.homes,
            "clock": lambda: T0,
            "source": self.source,
            "recorder": self.recorder,
            "projection_state": self.status_now,
            "projection_wait_seconds": 0.2,
            "projection_poll_seconds": 0.05,
            "schema_version": lambda: "0129",
        }
        values.update(options)
        return RecoveryBackupRunner(
            self.database, self.world.checkout, self.world.projection, **values
        )


class BackupRunTest(BackupTestCase):
    async def test_first_run_claims_the_empty_clone_commits_and_pushes(self) -> None:
        result = await self.runner().run()
        self.assertTrue(result.ok, result)
        self.assertTrue(result.committed)
        self.assertTrue(result.pushed)
        self.assertEqual(1, self.world.remote_commits())
        files = tree(self.world.checkout)
        self.assertEqual(MARKER_CONTENT, files[MARKER_NAME])
        self.assertIn("manifest.json", files)
        self.assertIn(f"memory/users/{self.owner_id}/{self.memory_id}.md", files)
        self.assertEqual(
            ("recovery.backup.completed", "files="),
            (self.recorder.rows[0][0], self.recorder.rows[0][1][:6]),
        )
        self.assertIn("commit=1 push=1", self.recorder.rows[0][1])
        author = git("log", "-1", "--format=%an <%ae>", cwd=self.world.checkout)
        self.assertEqual(
            "Personal AI Workspace <recovery@personal-ai-workspace.invalid>", author
        )

    async def test_modes_are_private(self) -> None:
        await self.runner().run()
        root = self.world.checkout
        self.assertEqual(0o700, stat.S_IMODE(os.stat(root).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(root / "users").st_mode))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(root / "manifest.json").st_mode))

    async def test_a_run_without_changes_commits_and_pushes_nothing(self) -> None:
        await self.runner().run()
        head = self.world.remote_head()
        later = await self.runner(clock=lambda: T0.replace(hour=13)).run()
        self.assertTrue(later.ok)
        self.assertFalse(later.committed)
        self.assertFalse(later.pushed)
        self.assertEqual(0, later.written)
        self.assertEqual(head, self.world.remote_head())
        self.assertIn("commit=0 push=0", self.recorder.rows[-1][1])

    async def test_changes_are_batched_into_one_commit(self) -> None:
        await self.runner().run()
        self.source.snapshot_value = replace(
            self.source.snapshot_value,
            users=(
                *self.source.snapshot_value.users,
                user(login_name="b"),
                user(login_name="c"),
            ),
        )
        result = await self.runner().run()
        self.assertTrue(result.committed)
        self.assertEqual(2, self.world.remote_commits())

    async def test_a_removed_entity_is_removed_from_the_current_backup(self) -> None:
        extra = user(login_name="extra")
        self.source.snapshot_value = replace(
            self.source.snapshot_value,
            users=(*self.source.snapshot_value.users, extra),
        )
        await self.runner().run()
        path = self.world.checkout / "users" / f"{extra['id']}.json"
        self.assertTrue(path.exists())
        self.source.snapshot_value = small_snapshot()
        result = await self.runner().run()
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.removed, 1)
        self.assertFalse(path.exists())
        tracked = git("ls-files", cwd=self.world.checkout)
        self.assertNotIn(f"users/{extra['id']}.json", tracked)

    async def test_unmanaged_files_are_left_alone_and_not_committed(self) -> None:
        readme = self.world.checkout / "README.md"
        await self.runner().run()
        readme.write_text("notes\n")
        result = await self.runner().run()
        self.assertTrue(result.ok)
        self.assertEqual("notes\n", readme.read_text())
        self.assertNotIn("README.md", git("ls-files", cwd=self.world.checkout))

    async def test_an_unrelated_staged_path_is_never_committed(self) -> None:
        await self.runner().run()
        head = self.world.remote_head()
        secret = self.world.checkout / "notes.txt"
        secret.write_text("not for the backup\n")
        git("add", "notes.txt", cwd=self.world.checkout)
        self.source.snapshot_value = snapshot_with(users=[user(login_name="other")])
        result = await self.runner().run()
        # The backup goes on with the managed names only; the staged file stays
        # staged, for a person, and never reaches the remote.
        self.assertTrue(result.ok, result)
        self.assertTrue(result.committed)
        self.assertNotEqual(head, self.world.remote_head())
        staged = git("diff", "--cached", "--name-only", cwd=self.world.checkout)
        self.assertEqual("notes.txt", staged)
        self.assertNotIn(
            "notes.txt",
            git("ls-tree", "-r", "--name-only", "HEAD", cwd=self.world.checkout),
        )

    async def test_the_commit_holds_the_rendered_bytes_only(self) -> None:
        # Whatever happens to the work tree or the index meanwhile (a file
        # rewritten after the write, a path staged by someone), the commit holds
        # exactly the files the backup rendered, and nothing else.
        await self.runner().run()
        checkout = self.world.checkout
        (checkout / "notes.txt").write_text("not for the backup\n")
        git("add", "notes.txt", cwd=checkout)
        name = next((checkout / "users").iterdir()).name
        (checkout / "users" / name).write_text('{"secret": "unrendered"}\n')
        rendered = {
            "users/x.json": b"{}\n",
            f"users/{name}": b'{"rendered": true}\n',
        }
        recovery_git = RecoveryGit(str(checkout))
        committed = recovery_git.commit_files(rendered, "Recovery backup\n", ("users",))
        self.assertTrue(committed)
        tree = git("ls-tree", "-r", "--name-only", "HEAD", cwd=checkout).split()
        self.assertNotIn("notes.txt", tree)
        self.assertEqual(
            {"users/x.json", f"users/{name}"},
            {path for path in tree if path.startswith("users/")},
        )
        self.assertIn("manifest.json", tree)  # other names are kept as they were
        self.assertEqual(
            '{"rendered": true}',
            git("show", f"HEAD:users/{name}", cwd=checkout),
        )
        self.assertEqual(
            "notes.txt", git("diff", "--cached", "--name-only", cwd=checkout)
        )
        self.assertFalse(recovery_git.commit_files(rendered, "again\n", ("users",)))

    async def test_hooks_of_the_checkout_never_run(self) -> None:
        hooks = self.world.checkout / ".git" / "hooks"
        witness = self.world.base / "hook-ran"
        for name in ("pre-commit", "commit-msg", "post-commit", "pre-push"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch {witness}\nexit 1\n")
            hook.chmod(0o700)
        result = await self.runner().run()
        self.assertTrue(result.ok, result)
        self.assertFalse(witness.exists())

    async def test_a_failed_push_is_retried_by_the_next_run(self) -> None:
        await self.runner().run()
        self.source.snapshot_value = snapshot_with(users=[user(login_name="later")])
        git(
            "config",
            "remote.origin.pushurl",
            str(self.world.base / "nowhere.git"),
            cwd=self.world.checkout,
        )
        failed = await self.runner().run()
        self.assertFalse(failed.ok)
        self.assertIs(BackupStep.PUSH, failed.failed_step)
        self.assertTrue(failed.committed)
        self.assertEqual(
            ("recovery.backup.failed", "push:command_failed"), self.recorder.rows[-1]
        )
        self.assertEqual(1, self.world.remote_commits())
        git("config", "--unset", "remote.origin.pushurl", cwd=self.world.checkout)
        retried = await self.runner().run()
        self.assertTrue(retried.ok)
        self.assertFalse(retried.committed)
        self.assertTrue(retried.pushed)
        self.assertEqual(2, self.world.remote_commits())

    async def test_a_diverged_remote_is_never_overwritten(self) -> None:
        await self.runner().run()
        other = self.world.clone("other")
        (other / "README.md").write_text("someone else\n")
        git("add", "README.md", cwd=other)
        git("commit", "-q", "-m", "elsewhere", cwd=other)
        git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=other)
        remote = self.world.remote_head()
        self.source.snapshot_value = snapshot_with(users=[user(login_name="new")])
        result = await self.runner().run()
        self.assertIs(BackupStep.PUSH, result.failed_step)
        self.assertEqual("push_rejected", result.error)
        self.assertEqual(remote, self.world.remote_head())

    async def test_no_upstream_fails_before_anything_is_written(self) -> None:
        git("config", "--unset", f"branch.{BRANCH}.merge", cwd=self.world.checkout)
        result = await self.runner().run()
        self.assertEqual(
            ("recovery.backup.failed", "check_repository:no_upstream"),
            self.recorder.rows[-1],
        )
        self.assertFalse(result.ok)
        self.assertEqual({}, tree(self.world.checkout))

    async def test_a_projection_that_did_not_complete_is_not_copied(self) -> None:
        self.status = replace(self.status, last_action="memory.projection.failed")
        result = await self.runner().run()
        self.assertEqual(
            ("recovery.backup.failed", "copy_memory:projection_not_completed"),
            self.recorder.rows[-1],
        )
        self.assertFalse(result.committed)
        self.assertEqual(0, self.world.remote_commits())
        self.assertEqual({MARKER_NAME}, set(tree(self.world.checkout)))

    async def test_an_incomplete_projection_is_not_copied(self) -> None:
        (self.world.projection / INCOMPLETE_NAME).write_bytes(b"x\n")
        result = await self.runner().run()
        self.assertEqual("projection_incomplete", result.error)
        self.assertIs(BackupStep.COPY_MEMORY, result.failed_step)

    async def test_a_busy_projection_is_waited_for_then_reported(self) -> None:
        import fcntl

        marker = os.open(self.world.projection / ".paw-memory-projection", os.O_RDONLY)
        self.addCleanup(os.close, marker)
        fcntl.flock(marker, fcntl.LOCK_EX)
        result = await self.runner().run()
        self.assertEqual("projection_busy", result.error)
        fcntl.flock(marker, fcntl.LOCK_UN)
        result = await self.runner().run()
        self.assertTrue(result.ok)

    async def test_a_second_run_at_the_same_time_does_nothing(self) -> None:
        checkout = open_checkout(
            str(self.world.checkout),
            self.world.homes,
            claim=RecoveryGit(str(self.world.checkout)).has_no_history,
        )
        self.addCleanup(checkout.close)
        with self.assertRaises(RecoveryBusyError):
            await self.runner().run()
        self.assertEqual([], self.recorder.rows)
        # A restore waits for nobody either (the command: exit 1).
        with self.assertRaises(RecoveryBusyError):
            await RecoveryRestorer(
                self.database, self.world.checkout, protected_homes=self.world.homes
            ).run()

    async def test_an_unrecorded_outcome_is_not_ok(self) -> None:
        async def failing(action, reason, *, occurred_at):
            raise OSError("down")

        result = await self.runner(recorder=failing).run()
        self.assertIsNone(result.failed_step)
        self.assertFalse(result.audited)
        self.assertFalse(result.ok)

    async def test_cancellation_is_recorded_then_propagates(self) -> None:
        started = asyncio.Event()

        class Slow:
            async def snapshot(self):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.ensure_future(self.runner(source=Slow()).run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            ("recovery.backup.failed", "read_database:CancelledError"),
            self.recorder.rows[-1],
        )
        # The lock was released: the next run proceeds.
        self.assertTrue((await self.runner().run()).ok)


class ObjectFormatTest(BackupTestCase):
    async def test_a_sha256_repository_can_be_committed_to(self) -> None:
        repository = self.world.base / "sha256"
        git(
            "init", "-q", "--object-format=sha256", str(repository), cwd=self.world.base
        )
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repository)
        recovery_git = RecoveryGit(str(repository))
        files = {"users/a.json": b"{}\n"}
        self.assertTrue(recovery_git.commit_files(files, "first\n", ("users",)))
        self.assertEqual(64, len(recovery_git.head()))
        self.assertEqual(
            files,
            recovery_git.read_commit_files(
                recovery_git.head(), ("users",), max_bytes=1024
            ),
        )
        self.assertFalse(recovery_git.commit_files(files, "again\n", ("users",)))


class RestoreSourceTest(BackupTestCase):
    async def test_the_source_is_read_from_the_verified_commit(self) -> None:
        # Status says clean (the change is hidden from it), but the files on
        # disk are not the commit: the restore reads the commit, never the
        # work tree.
        await self.runner().run()
        clone = self.world.clone()
        path = next((clone / "users").iterdir())
        original = path.read_bytes()
        relative = str(path.relative_to(clone))
        git("update-index", "--assume-unchanged", relative, cwd=clone)
        path.write_bytes(original.replace(b"alice", b"mallory"))
        self.assertEqual("", git("status", "--porcelain", cwd=clone))
        data = load_source(str(clone), self.world.homes)
        self.assertEqual(["alice"], [row["login_name"] for row in data.users])


class CheckoutTest(BackupTestCase):
    async def test_a_checkout_with_other_content_and_no_marker_is_refused(self) -> None:
        (self.world.checkout / "src.py").write_text("print()\n")
        result = await self.runner().run()
        self.assertEqual(
            "check_repository:not_recovery_repository", self.recorder.rows[-1][1]
        )
        self.assertFalse(result.ok)
        self.assertFalse((self.world.checkout / MARKER_NAME).exists())

    def project_clone(self, *options: str) -> tuple:
        """A project's remote with one commit, and a clone of it (``options``
        go to ``git clone``)."""
        project = self.world.base / "project.git"
        git("init", "--bare", "--quiet", str(project), cwd=self.world.base)
        seed = self.world.base / "seed"
        git("clone", "--quiet", str(project), str(seed), cwd=self.world.base)
        (seed / "src.py").write_text("print()\n")
        git("add", "src.py", cwd=seed)
        git("commit", "--quiet", "-m", "project", cwd=seed)
        git("push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=seed)
        before = git("rev-parse", BRANCH, cwd=project)
        clone = self.world.base / "project"
        git("clone", "--quiet", *options, str(project), str(clone), cwd=self.world.base)
        return project, clone, before

    async def assert_project_clone_refused(self, project, clone, before) -> None:
        self.assertEqual([".git"], os.listdir(clone))
        recorder = Recorder()
        values = {
            "protected_homes": self.world.homes,
            "clock": lambda: T0,
            "source": self.source,
            "recorder": recorder,
            "projection_state": self.status_now,
            "schema_version": lambda: "0129",
        }
        result = await RecoveryBackupRunner(
            self.database, clone, self.world.projection, **values
        ).run()
        self.assertEqual(
            ("recovery.backup.failed", "check_repository:not_recovery_repository"),
            recorder.rows[-1],
        )
        self.assertFalse(result.ok)
        self.assertEqual([".git"], os.listdir(clone))
        self.assertEqual(
            [f"refs/heads/{BRANCH}"],
            git("for-each-ref", "--format=%(refname)", cwd=project).split(),
        )
        self.assertEqual(before, git("rev-parse", BRANCH, cwd=project))

    async def test_a_project_clone_without_a_work_tree_is_refused(self) -> None:
        # ``git clone --no-checkout``: only ``.git`` at the root, but a history.
        await self.assert_project_clone_refused(*self.project_clone("--no-checkout"))

    async def test_a_project_clone_on_a_new_orphan_branch_is_refused(self) -> None:
        # No ``HEAD`` commit and nothing in the work tree, but the repository
        # has refs (the project's history): not an empty clone.
        project, clone, before = self.project_clone()
        git("checkout", "--quiet", "--orphan", "backup", cwd=clone)
        git("rm", "-r", "-q", "--cached", ".", cwd=clone)
        (clone / "src.py").unlink()
        git("config", "branch.backup.remote", "origin", cwd=clone)
        git("config", "branch.backup.merge", "refs/heads/backup", cwd=clone)
        await self.assert_project_clone_refused(project, clone, before)

    async def test_a_directory_inside_a_work_tree_is_refused(self) -> None:
        result = await self.runner().run()
        self.assertTrue(result.ok)
        inner = self.world.checkout / "inner"
        inner.mkdir()
        runner = RecoveryBackupRunner(
            self.database,
            inner,
            self.world.projection,
            protected_homes=self.world.homes,
            source=self.source,
            recorder=self.recorder,
            projection_state=self.status_now,
            schema_version=lambda: "0129",
        )
        await runner.run()
        self.assertEqual("check_repository:not_top_level", self.recorder.rows[-1][1])

    async def test_paths_that_are_refused(self) -> None:
        home_checkout = self.world.home / "recovery"
        home_checkout.mkdir()
        cases = {
            "not_absolute": "relative/path",
            "not_canonical": f"{self.world.checkout}/../recovery",
            "overlaps_home": str(home_checkout),
            "overlaps_projection": str(self.world.projection),
        }
        link = self.world.base / "link"
        link.symlink_to(self.world.checkout)
        cases["not_canonical_link"] = str(link)
        for name, path in cases.items():
            with self.subTest(name=name):
                recorder = Recorder()
                await RecoveryBackupRunner(
                    self.database,
                    path,
                    self.world.projection,
                    protected_homes=self.world.homes,
                    source=self.source,
                    recorder=recorder,
                    projection_state=self.status_now,
                    schema_version=lambda: "0129",
                ).run()
                expected = name.removesuffix("_link")
                self.assertEqual(f"check_repository:{expected}", recorder.rows[-1][1])

    async def test_a_link_in_a_managed_tree_is_replaced_never_followed(self) -> None:
        await self.runner().run()
        outside = self.world.base / "outside"
        outside.mkdir()
        users = self.world.checkout / "users"
        victim = next(users.iterdir())
        victim.unlink()
        victim.symlink_to(outside / "target.json")
        (self.world.checkout / "tasks").symlink_to(outside)
        result = await self.runner().run()
        self.assertTrue(result.ok, result)
        self.assertFalse(victim.is_symlink())
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((self.world.checkout / "tasks").is_symlink())

    async def test_projection_reader_copies_only_projection_names(self) -> None:
        (self.world.projection / "shared" / "notes.txt").write_text("x\n")
        (self.world.projection / "shared" / ".tmp-0123456789abcdef").write_text("x")
        (self.world.projection / "stray").mkdir()
        reader = open_projection(str(self.world.projection))
        try:
            files = reader.read()
        finally:
            reader.close()
        self.assertEqual(
            {
                f"users/{self.owner_id}/{self.memory_id}.md",
                f"users/{self.owner_id}/INDEX.md",
                "shared/INDEX.md",
            },
            set(files),
        )

    async def test_a_foreign_projection_marker_is_refused(self) -> None:
        (self.world.projection / ".paw-memory-projection").write_bytes(b"other\n")
        with self.assertRaises(RecoveryFilesError) as caught:
            open_projection(str(self.world.projection))
        self.assertIs(CheckoutProblem.MARKER_INVALID, caught.exception.problem)


class RestoreCancellationTest(BackupTestCase):
    """An interrupted restore (Ctrl-C, SIGTERM) leaves its audit row."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.assertTrue((await self.runner().run()).ok)
        self.clone = self.world.clone()
        self.rows: list[tuple[str, str]] = []
        self.release_record = asyncio.Event()
        self.release_record.set()
        self.recording = asyncio.Event()

        async def record(database, action, reason, *, occurred_at):
            self.recording.set()
            await self.release_record.wait()
            self.rows.append((str(action), reason))

        patcher = mock.patch(
            "paw_backend.recovery.restore.record_recovery_outcome", record
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def restorer(self, cls=RecoveryRestorer) -> RecoveryRestorer:
        return cls(self.database, self.clone, protected_homes=self.world.homes)

    def assert_lock_released(self) -> None:
        open_checkout(str(self.clone), self.world.homes).close()

    async def test_a_cancellation_while_the_source_is_read_is_recorded(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = restore_module.open_source

        def slow_open_source(*arguments, **options):
            started.set()
            release.wait(10)
            return original(*arguments, **options)

        with mock.patch.object(restore_module, "open_source", slow_open_source):
            task = asyncio.ensure_future(self.restorer().run(apply=True))
            await asyncio.to_thread(started.wait, 10)
            task.cancel()
            await asyncio.sleep(0.05)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([("recovery.restore.failed", "CancelledError")], self.rows)
        self.assert_lock_released()

    async def test_a_cancellation_while_restoring_is_recorded(self) -> None:
        started = asyncio.Event()

        class Slow(RecoveryRestorer):
            async def _restore(self, data, *, apply):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.ensure_future(self.restorer(Slow).run(apply=True))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([("recovery.restore.failed", "CancelledError")], self.rows)
        self.assert_lock_released()

    async def test_a_cancellation_while_the_outcome_is_recorded_adds_no_row(
        self,
    ) -> None:
        (self.clone / "stray.txt").write_text("x\n")  # not clean: refused
        self.release_record.clear()
        task = asyncio.ensure_future(self.restorer().run())
        await self.recording.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        self.release_record.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([("recovery.restore.refused", "not_clean")], self.rows)


if __name__ == "__main__":
    unittest.main()
