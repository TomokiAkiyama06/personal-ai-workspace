"""The repository tools in the application (#208, Decision 0083, 2, 6 and 11).

A deployment that runs agents on its repositories gives ``create_app`` a
:class:`SandboxSetup` (like ``ComputeSetup``: no environment setting; in
production the runtime configuration file's ``[sandbox]`` builds it,
``paw_backend/runtime_config.py``, Decision 0093): the sandbox runner
(``SshSandboxRunner`` through the forced-command wrapper, or
``LocalWrapperSandboxRunner`` for a single-user backend) and where the git
shims' sockets go. The composition then registers the repository tools next to
the Working Set's in the Tool Broker (:func:`repository_tools`), and the
lifespan stops idle sandboxes (:func:`reap_idle_sandboxes`) and every sandbox at
shutdown.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.git import GitRunner
from paw_backend.sandbox.gitread import GitReader
from paw_backend.sandbox.pool import DEFAULT_IDLE_SECONDS, SandboxPool
from paw_backend.sandbox.runner import SandboxRunner
from paw_backend.sandbox.tools import RepoToolExecutor

logger = logging.getLogger(__name__)

#: How often idle sandboxes are looked for.
REAP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class SandboxSetup:
    """``runner``: how a sandbox is started (``SandboxRunner``). ``shim_base``:
    the directory of the git shims' sockets (``None``: the sandbox's ``git`` is
    not available); ``shim_group``: the group the workspace accounts share with
    the backend for them (``None``: the backend is that account).
    ``idle_seconds``: an unused sandbox is stopped after this long."""

    runner: SandboxRunner
    shim_base: str | None = None
    shim_group: int | None = None
    idle_seconds: float = DEFAULT_IDLE_SECONDS

    def __post_init__(self) -> None:
        for name in ("start", "exec", "stop"):
            if not callable(getattr(self.runner, name, None)):
                raise TypeError("runner must be a SandboxRunner")
        if self.shim_base is not None and not os.path.isabs(self.shim_base):
            raise ValueError("shim_base must be an absolute path")


def repository_tools(
    setup: SandboxSetup,
    *,
    git_runner: GitRunner,
    accounts: AccountDirectory,
    oom_status: bool = False,
) -> tuple[RepoToolExecutor, SandboxPool]:
    """The executor of the repository tools and its sandbox pool. Its git shim
    still needs the tool runner (``RepoToolExecutor.bind_git_caller``).
    ``oom_status``: ``PAW_SANDBOX_OOM_STATUS`` (Decision 0092, 5)."""
    if not isinstance(setup, SandboxSetup):
        raise TypeError("sandbox must be a SandboxSetup")
    pool = SandboxPool(
        setup.runner,
        shim_base=setup.shim_base,
        shim_group=setup.shim_group,
        idle_seconds=setup.idle_seconds,
        oom_status=oom_status,
    )
    return RepoToolExecutor(pool, GitReader(git_runner), accounts), pool


async def reap_idle_sandboxes(
    pool: SandboxPool, stop: asyncio.Event, *, interval: float = REAP_INTERVAL_SECONDS
) -> None:
    """Stop idle sandboxes every ``interval`` until ``stop``; then every one."""
    try:
        while not stop.is_set():
            try:
                await pool.reap_idle()
            except Exception as error:  # the next round tries again
                logger.warning(
                    "Idle sandboxes were not stopped (%s)", type(error).__name__
                )
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass
    finally:
        await pool.close()
