"""Helpers for the Recovery Repository tests (PAW-047).

Everything is made below a fresh temporary directory (``RecoveryWorld``): a bare
repository standing in for the private remote, a clone of it as the checkout,
and a projection directory. Nothing touches a real server path, a home
directory or a real remote; git runs as the current user only.
"""

import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from paw_backend.memory.projection.records import ProjectionStatus
from paw_backend.memory.projection.writer import MARKER_CONTENT as PROJECTION_MARKER
from paw_backend.memory.projection.writer import MARKER_NAME as PROJECTION_MARKER_NAME
from paw_backend.recovery.source import RecoverySnapshot

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
BRANCH = "main"


def git(*arguments: str, cwd: Path) -> str:
    """Run git in ``cwd`` (tests only), without the caller's ``GIT_*`` variables."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
    )
    result = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


class RecoveryWorld:
    """A temporary remote, checkout and projection directory."""

    def __init__(self, test) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="paw-recovery-test-")
        test.addCleanup(self._directory.cleanup)
        self.base = Path(os.path.realpath(self._directory.name))
        self.remote = self.base / "remote.git"
        self.checkout = self.base / "recovery"
        self.projection = self.base / "memory"
        # A fake home directory (never a real one): the checkout must not be in it.
        self.home = self.base / "home" / "someone"
        self.home.mkdir(parents=True)
        git("init", "--bare", "--quiet", str(self.remote), cwd=self.base)
        git("clone", "--quiet", str(self.remote), str(self.checkout), cwd=self.base)
        self.track(self.checkout)

    @property
    def homes(self) -> tuple[str, ...]:
        return (str(self.home),)

    @staticmethod
    def track(checkout: Path) -> None:
        """Point ``main`` at ``origin/main`` (an empty clone has no upstream)."""
        git("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}", cwd=checkout)
        git("config", f"branch.{BRANCH}.remote", "origin", cwd=checkout)
        git("config", f"branch.{BRANCH}.merge", f"refs/heads/{BRANCH}", cwd=checkout)

    def clone(self, name: str = "restore") -> Path:
        target = self.base / name
        git("clone", "--quiet", str(self.remote), str(target), cwd=self.base)
        return target

    def remote_head(self) -> str | None:
        try:
            return git("rev-parse", "--verify", "--quiet", BRANCH, cwd=self.remote)
        except subprocess.CalledProcessError:
            return None

    def remote_commits(self) -> int:
        if self.remote_head() is None:
            return 0
        return int(git("rev-list", "--count", BRANCH, cwd=self.remote))

    def write_projection(self, files: dict[str, bytes]) -> None:
        """A projection directory as the projection writer leaves it."""
        self.projection.mkdir(mode=0o700, exist_ok=True)
        (self.projection / PROJECTION_MARKER_NAME).write_bytes(PROJECTION_MARKER)
        for relative, data in files.items():
            path = self.projection / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def completed_projection() -> ProjectionStatus:
    return ProjectionStatus(
        last_action="memory.projection.completed",
        last_run_at=T0,
        last_reason="memories=1 written=1 removed=0 redacted=0",
        last_completed_at=T0,
        last_completed_recorded_at=T0,
        checked_at=T0,
    )


class StaticSource:
    """A ``SnapshotSource`` returning ``snapshot`` (changeable between runs)."""

    def __init__(self, snapshot: RecoverySnapshot) -> None:
        self.snapshot_value = snapshot

    async def snapshot(self) -> RecoverySnapshot:
        return self.snapshot_value


class Recorder:
    """An ``OutcomeRecorder`` that keeps the rows."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    async def __call__(self, action, reason: str, *, occurred_at) -> None:
        self.rows.append((str(action), reason))


def user(**overrides) -> dict:
    values = {
        "id": uuid4(),
        "login_name": "alice",
        "system_role": "user",
        "status": "active",
        "passkey_required": False,
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(overrides)
    return values


def version(memory_id: UUID, **overrides) -> dict:
    values = {
        "id": uuid4(),
        "memory_id": memory_id,
        "version_number": 1,
        "scope": "shared",
        "owner_user_id": None,
        "project_id": None,
        "project_group_id": None,
        "repo_id": None,
        "memory_type": "fact",
        "title": "A fact",
        "content": "Something true.",
        "importance": 50,
        "pinned": False,
        "status": "active",
        "confirmation_state": "confirmed",
        "freshness_policy": "permanent",
        "verified_at": None,
        "revalidate_after": None,
        "revalidate_triggers": [],
        "on_stale": "lower_priority",
        "expires_at": None,
        "commit_sha": None,
        "branch": None,
        "stale_since": None,
        "attributes": {},
        "actor_type": "system",
        "actor_user_id": None,
        "change_reason": None,
        "created_at": T0,
    }
    values.update(overrides)
    return values


def snapshot_with(*, users=(), memories=(), versions=(), **others) -> RecoverySnapshot:
    return RecoverySnapshot(
        users=tuple(users),
        memories=tuple(memories),
        versions=tuple(versions),
        **{key: tuple(value) for key, value in others.items()},
    )


def small_snapshot() -> RecoverySnapshot:
    memory_id = uuid4()
    return snapshot_with(
        users=[user()],
        memories=[{"id": memory_id, "created_at": T0}],
        versions=[version(memory_id)],
    )


def tree(root: Path) -> dict[str, bytes]:
    """Every regular file below ``root`` except ``.git`` (relative path -> bytes)."""
    files = {}
    for directory, directories, names in os.walk(root):
        if ".git" in directories:
            directories.remove(".git")
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(root))] = path.read_bytes()
    return files
