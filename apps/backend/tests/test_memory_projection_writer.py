"""The safe writer of the Memory Projection (PAW-045), on temporary directories.

Never into a git work tree or a home directory, never into a directory that is
not the projection's, ``0700`` / ``0600`` whatever the umask, no symbolic link
followed, only its own files replaced or removed, one run at a time.
Every test writes below its own ``tempfile`` directory only.
"""

import os
import threading
import unittest
from pathlib import Path

from paw_backend.memory.projection import (
    INDEX_FILE,
    MARKER_NAME,
    ProjectionBusyError,
    ProjectionTargetError,
    TargetProblem,
    open_target,
    render_projection,
)
from paw_backend.memory.projection.writer import MARKER_CONTENT

from .projection_support import TemporaryRoot, memory, mode, moved, tree


class WriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryRoot(self)
        self.root = self.tmp.root

    def open(self, root: Path | str | None = None):
        target = open_target(root or self.root, self.tmp.homes)
        self.addCleanup(target.close)
        return target

    def sync(self, memories):
        target = open_target(self.root, self.tmp.homes)
        try:
            return target.sync(render_projection(memories))
        finally:
            target.close()

    def refused(self, root, problem: TargetProblem) -> None:
        with self.assertRaises(ProjectionTargetError) as caught:
            open_target(root, self.tmp.homes)
        self.assertEqual(caught.exception.problem, problem)
        # The error names no path.
        self.assertNotIn(str(self.tmp.base), str(caught.exception))


class LayoutTest(WriterTestCase):
    def test_the_first_run_creates_a_private_tree_with_a_marker(self):
        mine = memory()
        project = memory(scope="project")
        shared = memory(scope="shared")
        report = self.sync([mine, project, shared])
        self.assertEqual(report.written, 6)
        self.assertEqual(
            (report.unchanged, report.removed, report.unmanaged), (0, 0, 0)
        )
        files = tree(self.root)
        self.assertEqual(files[MARKER_NAME], MARKER_CONTENT)
        user_dir = f"users/{mine.owner_user_id}"
        self.assertIn(f"{user_dir}/{mine.memory_id}.md", files)
        self.assertIn(f"{user_dir}/{INDEX_FILE}", files)
        self.assertIn(f"projects/{project.project_id}/{project.memory_id}.md", files)
        self.assertIn(f"shared/{shared.memory_id}.md", files)
        self.assertEqual(mode(self.root), 0o700)
        for directory, _, names in os.walk(self.root):
            self.assertEqual(mode(Path(directory)), 0o700, directory)
            for name in names:
                self.assertEqual(mode(Path(directory) / name), 0o600, name)

    def test_the_modes_do_not_depend_on_the_umask(self):
        old = os.umask(0o000)
        try:
            self.sync([memory()])
        finally:
            os.umask(old)
        for directory, _, names in os.walk(self.root):
            self.assertEqual(mode(Path(directory)), 0o700)
            for name in names:
                self.assertEqual(mode(Path(directory) / name), 0o600)

    def test_looser_modes_are_tightened(self):
        value = memory()
        self.sync([value])
        user_dir = self.root / "users" / str(value.owner_user_id)
        os.chmod(self.root, 0o755)
        os.chmod(user_dir, 0o755)
        os.chmod(user_dir / f"{value.memory_id}.md", 0o644)
        report = self.sync([value])
        self.assertEqual(report.written, 1)  # rewritten with 0600
        self.assertEqual(mode(self.root), 0o700)
        self.assertEqual(mode(user_dir), 0o700)
        self.assertEqual(mode(user_dir / f"{value.memory_id}.md"), 0o600)

    def test_a_second_run_without_changes_touches_nothing(self):
        values = [memory(), memory(scope="shared")]
        self.sync(values)
        before = {
            path: os.stat(self.root / path).st_mtime_ns for path in tree(self.root)
        }
        report = self.sync(values)
        self.assertEqual(report.written, 0)
        self.assertEqual(report.unchanged, 4)
        after = {
            path: os.stat(self.root / path).st_mtime_ns for path in tree(self.root)
        }
        self.assertEqual(before, after)

    def test_a_change_rewrites_only_that_memory_and_its_index(self):
        a, b = memory(), memory()
        self.sync([a, b])
        report = self.sync([moved(a, title="renamed", version_number=2), b])
        self.assertEqual(report.written, 2)  # a's file and a's INDEX.md
        self.assertEqual(report.unchanged, 2)

    def test_a_memory_that_moved_to_another_audience_leaves_the_old_one(self):
        project = memory(scope="project")
        self.sync([project])
        narrowed = moved(
            project, scope="user", project_id=None, owner_user_id=memory().owner_user_id
        )
        report = self.sync([narrowed])
        self.assertEqual(report.removed, 2)  # the file and the project's index
        files = tree(self.root)
        self.assertFalse(any(path.startswith("projects/") for path in files))
        self.assertFalse((self.root / "projects").exists())
        self.assertIn(f"users/{narrowed.owner_user_id}/{narrowed.memory_id}.md", files)

    def test_an_empty_projection_removes_every_memory_file(self):
        self.sync([memory(), memory(scope="shared")])
        report = self.sync([])
        self.assertEqual(report.removed, 4)
        self.assertEqual(set(tree(self.root)), {MARKER_NAME})


class UnmanagedTest(WriterTestCase):
    def test_entries_that_are_not_the_projections_are_left_alone(self):
        value = memory()
        self.sync([value])
        user_dir = self.root / "users" / str(value.owner_user_id)
        (self.root / "README.txt").write_text("kept")
        (self.root / "users" / "notes").mkdir()
        (user_dir / "notes.txt").write_text("kept")
        report = self.sync([])
        self.assertEqual(report.unmanaged, 3)
        self.assertEqual((self.root / "README.txt").read_text(), "kept")
        self.assertEqual((user_dir / "notes.txt").read_text(), "kept")
        self.assertFalse((user_dir / f"{value.memory_id}.md").exists())

    def test_leftover_temporary_files_are_cleaned_up(self):
        value = memory()
        self.sync([value])
        user_dir = self.root / "users" / str(value.owner_user_id)
        (user_dir / ".tmp-deadbeef").write_text("half")
        report = self.sync([value])
        self.assertFalse((user_dir / ".tmp-deadbeef").exists())
        self.assertEqual(report.removed, 0)


class RootTest(WriterTestCase):
    def test_a_non_empty_directory_without_the_marker_is_refused(self):
        self.root.mkdir()
        (self.root / "something.md").write_text("not ours")
        self.refused(self.root, TargetProblem.NOT_EMPTY)
        self.assertEqual(os.listdir(self.root), ["something.md"])

    def test_a_refused_directory_keeps_its_permissions(self):
        self.root.mkdir(mode=0o755)
        os.chmod(self.root, 0o755)
        (self.root / "something.md").write_text("not ours")
        self.refused(self.root, TargetProblem.NOT_EMPTY)
        self.assertEqual(mode(self.root), 0o755)

    def test_a_directory_with_a_tampered_marker_keeps_its_permissions(self):
        self.sync([])
        (self.root / MARKER_NAME).write_bytes(b"something else\n")
        os.chmod(self.root, 0o755)
        self.refused(self.root, TargetProblem.MARKER_INVALID)
        self.assertEqual(mode(self.root), 0o755)

    def test_an_existing_empty_directory_is_claimed(self):
        self.root.mkdir(mode=0o755)
        self.open()
        self.assertEqual(os.listdir(self.root), [MARKER_NAME])
        self.assertEqual(mode(self.root), 0o700)

    def test_a_tampered_marker_is_refused(self):
        self.sync([])
        (self.root / MARKER_NAME).write_bytes(b"something else\n")
        self.refused(self.root, TargetProblem.MARKER_INVALID)

    def test_a_directory_inside_a_git_work_tree_is_refused(self):
        checkout = self.tmp.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (checkout / "docs").mkdir()
        self.refused(checkout / "docs" / "memory", TargetProblem.INSIDE_GIT_WORK_TREE)
        self.assertFalse((checkout / "docs" / "memory").exists())
        # A .git file (a worktree or submodule) counts as well.
        worktree = self.tmp.base / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: elsewhere\n")
        self.refused(worktree / "memory", TargetProblem.INSIDE_GIT_WORK_TREE)
        self.refused(worktree, TargetProblem.INSIDE_GIT_WORK_TREE)
        # So does a .git symbolic link, whatever it points to.
        linked = self.tmp.base / "linked"
        linked.mkdir()
        (linked / ".git").symlink_to(self.tmp.base / "nowhere")
        self.refused(linked / "memory", TargetProblem.INSIDE_GIT_WORK_TREE)

    def test_an_empty_git_directory_is_not_a_repository(self):
        # git itself says "not a git repository" for it.
        base = self.tmp.base / "plain"
        (base / ".git").mkdir(parents=True)
        self.open(base / "memory")
        self.assertTrue((base / "memory" / MARKER_NAME).exists())

    def test_a_directory_in_or_above_a_home_is_refused(self):
        self.refused(self.tmp.home / "memory", TargetProblem.OVERLAPS_HOME)
        self.refused(self.tmp.home, TargetProblem.OVERLAPS_HOME)
        self.refused(self.tmp.home.parent, TargetProblem.OVERLAPS_HOME)
        self.assertEqual(os.listdir(self.tmp.home), [])

    def test_paths_that_are_not_absolute_and_canonical_are_refused(self):
        self.refused("memory", TargetProblem.NOT_ABSOLUTE)
        self.refused(f"{self.tmp.base}/x/../memory", TargetProblem.NOT_CANONICAL)
        self.refused(f"{self.tmp.base}/memory/", TargetProblem.NOT_CANONICAL)
        self.refused("/", TargetProblem.FILESYSTEM_ROOT)
        self.refused(self.tmp.base / "missing" / "memory", TargetProblem.PARENT_MISSING)

    def test_a_symbolic_link_anywhere_in_the_path_is_refused(self):
        real = self.tmp.base / "real"
        real.mkdir()
        link = self.tmp.base / "link"
        link.symlink_to(real)
        self.refused(link, TargetProblem.NOT_CANONICAL)
        self.refused(link / "memory", TargetProblem.NOT_CANONICAL)
        self.assertEqual(os.listdir(real), [])

    def test_a_file_is_not_a_directory(self):
        self.root.write_text("x")
        self.refused(self.root, TargetProblem.NOT_A_DIRECTORY)

    def test_a_second_run_at_the_same_time_is_busy(self):
        self.open()
        with self.assertRaises(ProjectionBusyError):
            open_target(self.root, self.tmp.homes)

    def test_the_lock_is_released_on_close(self):
        target = open_target(self.root, self.tmp.homes)
        target.close()
        target.close()  # idempotent
        self.open()

    def test_the_lock_holds_across_threads(self):
        self.open()
        errors = []

        def other():
            try:
                open_target(self.root, self.tmp.homes).close()
            except ProjectionBusyError as error:
                errors.append(error)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)


class LinkTest(WriterTestCase):
    def test_a_link_where_a_directory_belongs_fails_and_is_not_followed(self):
        value = memory()
        self.sync([])
        outside = self.tmp.base / "outside"
        outside.mkdir()
        (self.root / "users").symlink_to(outside)
        with self.assertRaises(ProjectionTargetError) as caught:
            self.sync([value])
        self.assertEqual(caught.exception.problem, TargetProblem.UNSAFE_ENTRY)
        self.assertEqual(os.listdir(outside), [])

    def test_a_link_where_a_user_directory_belongs_fails(self):
        value = memory()
        self.sync([memory()])
        outside = self.tmp.base / "outside"
        outside.mkdir()
        (self.root / "users" / str(value.owner_user_id)).symlink_to(outside)
        with self.assertRaises(ProjectionTargetError):
            self.sync([value])
        self.assertEqual(os.listdir(outside), [])

    def test_a_link_in_place_of_a_file_is_replaced_not_written_through(self):
        value = memory()
        self.sync([value])
        user_dir = self.root / "users" / str(value.owner_user_id)
        victim = self.tmp.base / "victim.txt"
        victim.write_text("original")
        path = user_dir / f"{value.memory_id}.md"
        path.unlink()
        path.symlink_to(victim)
        report = self.sync([value])
        self.assertEqual(report.written, 1)
        self.assertEqual(victim.read_text(), "original")
        self.assertFalse(path.is_symlink())
        self.assertEqual(mode(path), 0o600)

    def test_a_hard_link_is_replaced_not_written_through(self):
        value = memory()
        self.sync([value])
        path = self.root / "users" / str(value.owner_user_id) / f"{value.memory_id}.md"
        other = self.tmp.base / "other-name"
        os.link(path, other)
        self.sync([moved(value, content="changed", version_number=2)])
        self.assertNotIn(b"changed", other.read_bytes())
        self.assertIn(b"changed", path.read_bytes())

    def test_a_stale_link_is_removed_not_its_target(self):
        value = memory()
        self.sync([value])
        user_dir = self.root / "users" / str(value.owner_user_id)
        victim = self.tmp.base / "victim.txt"
        victim.write_text("original")
        stray = memory(owner_user_id=value.owner_user_id)
        (user_dir / f"{stray.memory_id}.md").symlink_to(victim)
        report = self.sync([value])
        self.assertEqual(report.removed, 1)
        self.assertEqual(victim.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
