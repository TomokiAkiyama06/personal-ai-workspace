"""Who makes a managed checkout's directory (Issue #285, Decision 0097).

A managed checkout (a GitHub clone, a new repository, a member's own clone) is a
new directory ``<home>/<workspace_subdir>/<project directory>/<repository>``
(Decision 0017 §1). :class:`CheckoutDirectories` is the seam that makes it,
and removes it again when the registration fails:

* :class:`LocalCheckoutDirectories` -- the backend process itself
  (``paths.create_checkout_directory`` / ``paths.remove_directory``), for a
  backend that runs as every checkout's own Linux user (``SubprocessGitRunner``).
* ``paw_backend.repositories.ssh.SshCheckoutDirectories`` -- the account itself,
  through the forced-command wrapper (``paw-dir/v1``), for a backend that
  reaches its accounts over SSH (``SshGitRunner``, Decision 0029): its own
  Linux user can neither create a directory in another user's home nor make one
  that would be the account's.

``RepositoryService.from_policy`` picks one from the ``GitRunner``. Checking a
path (``paths.plan_checkout_path``, ``read_checkout_identity``,
``inspect_checkout_root``) stays in the backend process either way, so the
backend's user must be able to traverse to a checkout (Decision 0097, 4).
"""

import asyncio
from typing import Protocol

from paw_backend.repositories.paths import (
    LinuxAccount,
    create_checkout_directory,
    remove_directory,
)


class CheckoutDirectories(Protocol):
    """Makes and removes a managed checkout's directory as its account."""

    async def create(self, path: str, account: LinuxAccount) -> None:
        """Make the new, empty directory ``path`` (``paths.plan_checkout_path``).

        ``PathRejectedError`` when the path cannot be used (``EXISTS`` for
        anything already there: never reused)."""
        ...

    async def remove(self, path: str, account: LinuxAccount) -> bool:
        """Remove what ``create`` made for a registration that failed. Best
        effort, never raises: whether the directory is gone afterwards."""
        ...


class LocalCheckoutDirectories:
    """The backend process makes the directories itself (Decision 0017 §1)."""

    async def create(self, path: str, account: LinuxAccount) -> None:
        await asyncio.to_thread(create_checkout_directory, path, account)

    async def remove(self, path: str, account: LinuxAccount) -> bool:
        return await asyncio.to_thread(remove_directory, path, account)
