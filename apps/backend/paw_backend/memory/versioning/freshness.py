"""Freshness maintenance (PAW-042): stale candidates, expiry, session and task end.

MEMORY_ARCHITECTURE.md section 11 and REQUIREMENTS.md "Memory Freshness /
Revalidate Policy": there is no single TTL, and reaching ``revalidate_after`` never
invalidates a memory. What each policy leads to (Decision 0034, 4 and 5):

* ``revalidate``: when ``verified_at + revalidate_after`` has passed
  (:meth:`FreshnessMaintenance.mark_revalidation_due`), or an event it waits for
  happened (:meth:`~FreshnessMaintenance.mark_triggered`), the version becomes a
  **stale candidate**: ``stale_since`` is set, the status stays ``active``. The
  retrieval (PAW-043) still offers it, marked stale and with a lower score
  (``on_stale: lower_priority``), so that it is re-checked when it is needed; the
  person then revalidates, edits or deprecates it (``MemoryVersioningService``).
* ``repo_commit``: stale when the repository's head moved away from the memory's
  commit (:meth:`~FreshnessMaintenance.mark_repo_head`). A memory of another branch
  is not judged by this branch's head. Re-analysis writes a new version later.
* ``expiring``: gone at ``expires_at``. The retrieval already leaves an expired
  version out; :meth:`~FreshnessMaintenance.expire_due` also sets it ``deprecated``
  so that the Memory UI shows it as ended.
* ``session_only``: never Long-term Memory (REQUIREMENTS.md: not kept after a
  Session or a Task ends). The retrieval never offers it;
  :meth:`~FreshnessMaintenance.end_session` sets the ones that came from a
  conversation (a ``memory_sources`` row naming it) ``deprecated`` when the session
  ends, and :meth:`~FreshnessMaintenance.end_task` the ones that came from a task
  (a ``task`` source whose ``source_ref`` is the task id's canonical text,
  ``str(task_id)``) when the task ends. Nothing is erased: erasing belongs to the
  conversation deletion flow.
* ``permanent``: nothing.

Every method is backend-internal (a scheduled job or an event handler): there is
no caller to authorize (the ``system`` actor; the jobs UPDATE and count and read no
content, so the ACL of ``memory/acl.py`` has no reader to apply to), and the
changes are recorded by the database in ``memory_metadata_changes`` with the
``system`` actor. Each call changes at most
``batch`` versions (``FOR UPDATE SKIP LOCKED``: a version a person is editing is
left for the next call) and returns how many it changed; a job repeats a call until
it returns 0. A version is never marked twice (``stale_since IS NULL``), so running
a call again changes nothing. A database error leaves as
:class:`~paw_backend.memory.versioning.errors.MemoryDatabaseError`, detached from
the driver's error (whose text can quote a memory).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, or_, select, text, update
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.memory.metadata import metadata_change_actor
from paw_backend.memory.models import (
    ActorType,
    FreshnessPolicy,
    MemorySource,
    MemoryStatus,
    MemoryVersion,
    SourceType,
)
from paw_backend.memory.versioning import limits
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryDatabaseError,
    raise_detached,
)
from paw_backend.memory.versioning.records import (
    RevalidateTrigger,
    TargetKind,
    TriggerTarget,
)
from paw_backend.memory.versioning.validation import (
    reject,
    validate_aware_datetime,
    validate_commit_sha,
    validate_enum,
    validate_int,
    validate_optional_text,
    validate_uuid,
)

Clock = Callable[[], datetime]

_V = MemoryVersion.__table__
_S = MemorySource.__table__
_ACTIVE = MemoryStatus.ACTIVE.value


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FreshnessMaintenance:
    """The freshness jobs over ``memory_versions`` (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = _utc_now,
        batch: int = limits.DEFAULT_SWEEP_BATCH,
    ) -> None:
        if not isinstance(database, Database):
            raise reject("database", InputProblem.WRONG_TYPE)
        if not callable(clock):
            raise reject("clock", InputProblem.WRONG_TYPE)
        self._database = database
        self._clock = clock
        self._batch = validate_int("batch", batch, low=1, high=limits.MAX_SWEEP_BATCH)

    @property
    def batch(self) -> int:
        """The most versions one call changes (a job that changed fewer is done)."""
        return self._batch

    def _now(self) -> datetime:
        return validate_aware_datetime("clock", self._clock())

    async def _apply(
        self, condition: ColumnElement[bool], values: dict[str, object]
    ) -> int:
        """Set ``values`` on at most ``batch`` active versions meeting ``condition``."""
        # A CTE that locks is evaluated exactly once. As an ``IN (subquery)`` the
        # planner may run the subquery again per row, and its LIMIT then bounds each
        # run instead of the call.
        chosen = (
            select(_V.c.id)
            .where(_V.c.status == _ACTIVE, condition)
            .order_by(_V.c.id)
            .limit(self._batch)
            .with_for_update(skip_locked=True)
            .cte("chosen")
        )
        failure: MemoryDatabaseError | None = None
        try:
            async with self._database.session() as session, session.begin():
                await self._prepare(session)
                result = await session.execute(
                    update(_V)
                    .where(_V.c.id.in_(select(chosen.c.id)), _V.c.status == _ACTIVE)
                    .values(**values)
                )
                count = result.rowcount
        except StatementError as error:
            # The driver's text can quote the row (``errors`` module): only the
            # SQLSTATE is kept, and the original is not linked.
            orig = error.orig if isinstance(error, DBAPIError) else None
            sqlstate = getattr(orig, "sqlstate", None)
            failure = MemoryDatabaseError(
                sqlstate if isinstance(sqlstate, str) else None
            )
        if failure is not None:
            raise_detached(failure)
        return count

    @staticmethod
    async def _prepare(session: AsyncSession) -> None:
        # ``verified_at + revalidate_after`` in UTC, where a day is 24 hours (as in
        # ``timedelta`` and the retrieval's own rule).
        await session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        # The database records every change with its actor (revision 0071).
        await session.execute(metadata_change_actor(ActorType.SYSTEM))

    async def mark_revalidation_due(self) -> int:
        """``revalidate`` versions past ``verified_at + revalidate_after``: stale."""
        now = self._now()
        return await self._apply(
            and_(
                _V.c.freshness_policy == FreshnessPolicy.REVALIDATE.value,
                _V.c.stale_since.is_(None),
                or_(
                    _V.c.verified_at.is_(None),
                    _V.c.revalidate_after.is_(None),
                    _V.c.verified_at + _V.c.revalidate_after <= now,
                ),
            ),
            {"stale_since": now},
        )

    async def mark_triggered(
        self, trigger: RevalidateTrigger, target: TriggerTarget
    ) -> int:
        """``revalidate`` versions that wait for ``trigger`` and ``target`` concerns."""
        trigger = validate_enum("trigger", trigger, RevalidateTrigger)
        if not isinstance(target, TriggerTarget):
            raise reject("target", InputProblem.WRONG_TYPE)
        now = self._now()
        scoped: ColumnElement[bool]
        if target.kind is TargetKind.USER:
            scoped = _V.c.owner_user_id == target.id
        elif target.kind is TargetKind.PROJECT:
            scoped = _V.c.project_id == target.id
        elif target.kind is TargetKind.REPO:
            scoped = _V.c.repo_id == target.id
        else:
            scoped = _V.c.id.is_not(None)  # the whole workspace
        return await self._apply(
            and_(
                _V.c.freshness_policy == FreshnessPolicy.REVALIDATE.value,
                _V.c.stale_since.is_(None),
                _V.c.revalidate_triggers.any(trigger.value),
                scoped,
            ),
            {"stale_since": now},
        )

    async def mark_repo_head(
        self, repo_id: UUID, commit_sha: str, *, branch: str | None = None
    ) -> int:
        """``repo_commit`` versions of ``repo_id`` whose commit is not the head.

        ``branch`` is the branch whose head ``commit_sha`` is: a version recorded
        for another branch is left alone (one without a branch is judged). Without
        ``branch`` every version of the repository is judged.
        """
        repo_id = validate_uuid("repo_id", repo_id)
        commit_sha = validate_commit_sha("commit_sha", commit_sha)
        branch = validate_optional_text(
            "branch", branch, max_chars=limits.MAX_BRANCH_CHARS
        )
        now = self._now()
        conditions = [
            _V.c.freshness_policy == FreshnessPolicy.REPO_COMMIT.value,
            _V.c.stale_since.is_(None),
            _V.c.repo_id == repo_id,
            _V.c.commit_sha.is_distinct_from(commit_sha),
        ]
        if branch is not None:
            conditions.append(or_(_V.c.branch.is_(None), _V.c.branch == branch))
        return await self._apply(and_(*conditions), {"stale_since": now})

    async def expire_due(self) -> int:
        """``expiring`` versions at or past ``expires_at``: ``deprecated``."""
        now = self._now()
        return await self._apply(
            and_(
                _V.c.freshness_policy == FreshnessPolicy.EXPIRING.value,
                or_(_V.c.expires_at.is_(None), _V.c.expires_at <= now),
            ),
            {"status": MemoryStatus.DEPRECATED.value},
        )

    async def end_session(self, conversation_id: UUID) -> int:
        """``session_only`` versions from ``conversation_id``: ``deprecated``."""
        conversation_id = validate_uuid("conversation_id", conversation_id)
        from_conversation = exists(
            select(_S.c.id).where(
                _S.c.memory_version_id == _V.c.id,
                _S.c.conversation_id == conversation_id,
            )
        )
        return await self._apply(
            and_(
                _V.c.freshness_policy == FreshnessPolicy.SESSION_ONLY.value,
                from_conversation,
            ),
            {"status": MemoryStatus.DEPRECATED.value},
        )

    async def end_task(self, task_id: UUID) -> int:
        """``session_only`` versions from the task ``task_id``: ``deprecated``.

        A task source names its task by ``source_ref`` (the database has no foreign
        key to ``tasks``): the canonical text of the id, ``str(task_id)``. Another
        spelling names no task here.
        """
        task_id = validate_uuid("task_id", task_id)
        from_task = exists(
            select(_S.c.id).where(
                _S.c.memory_version_id == _V.c.id,
                _S.c.source_type == SourceType.TASK.value,
                _S.c.source_ref == str(task_id),
            )
        )
        return await self._apply(
            and_(
                _V.c.freshness_policy == FreshnessPolicy.SESSION_ONLY.value,
                from_task,
            ),
            {"status": MemoryStatus.DEPRECATED.value},
        )
