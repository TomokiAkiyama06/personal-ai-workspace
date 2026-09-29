"""Helpers for the Memory Markdown Projection tests (PAW-045).

Every test writes only below a fresh temporary directory (``TemporaryRoot``);
nothing touches a real HDD mount, a home directory or a repository checkout.
"""

import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from paw_backend.memory.projection import ProjectedMemory

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def memory(**overrides) -> ProjectedMemory:
    """A current version of a user memory with ``overrides`` applied."""
    values = {
        "memory_id": uuid4(),
        "version_number": 1,
        "scope": "user",
        "owner_user_id": uuid4(),
        "project_id": None,
        "project_group_id": None,
        "repo_id": None,
        "memory_type": "preference",
        "title": "Prefers tabs",
        "content": "Use tabs for indentation.",
        "importance": 50,
        "pinned": False,
        "status": "active",
        "confirmation_state": "confirmed",
        "freshness_policy": "permanent",
        "verified_at": None,
        "revalidate_after": None,
        "revalidate_triggers": (),
        "expires_at": None,
        "commit_sha": None,
        "branch": None,
        "stale_since": None,
        "created_at": T0,
    }
    scope = overrides.get("scope")
    if scope is not None and scope != "user" and "owner_user_id" not in overrides:
        values["owner_user_id"] = None
        key = {
            "project": "project_id",
            "project_group": "project_group_id",
            "repo": "repo_id",
        }.get(scope)
        if key is not None and key not in overrides:
            values[key] = uuid4()
    values.update(overrides)
    return ProjectedMemory(**values)


def moved(value: ProjectedMemory, **changes) -> ProjectedMemory:
    return replace(value, **changes)


class TemporaryRoot:
    """A temporary base directory; ``root`` is a not-yet-existing child of it."""

    def __init__(self, test) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="paw-projection-test-")
        test.addCleanup(self._directory.cleanup)
        self.base = Path(os.path.realpath(self._directory.name))
        self.root = self.base / "memory"
        # A fake home directory (never a real one): the writer must refuse it.
        self.home = self.base / "home" / "someone"
        self.home.mkdir(parents=True)

    @property
    def homes(self) -> tuple[str, ...]:
        return (str(self.home),)


def tree(root: Path) -> dict[str, bytes]:
    """Every regular file below ``root`` (relative path -> bytes)."""
    files = {}
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(root))] = path.read_bytes()
    return files


def mode(path: Path) -> int:
    return os.lstat(path).st_mode & 0o7777


def uuid_text(value: UUID | None) -> str:
    assert value is not None
    return str(value)
