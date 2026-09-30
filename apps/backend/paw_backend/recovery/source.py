"""Reading what the Recovery Repository holds from PostgreSQL (PAW-047).

One call reads everything in one ``REPEATABLE READ, READ ONLY`` transaction, so
the files of one backup are one consistent snapshot. Every statement names its
columns: the list **is** the allow-list of what can reach Git (Decision 0054 3).
Tables that hold credentials or runtime state are never read here: password
hashes, Passkeys, sessions, setup / reset / invitation / pairing tokens,
``shared_connections.secret_handle``, conversations and messages, embeddings,
checkouts (paths in home directories), task inputs and logs, usage, audit.

Like the Memory Projection (Decision 0038 7) this is a backend-internal job, not
a principal's request: it reads every scope; the file modes of the checkout
(``0700`` / ``0600``) and the privacy of the remote keep it to the Owner.

A database error leaves as ``RecoveryDatabaseError`` with only the SQLSTATE,
detached from the driver's error (whose text can quote a row).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, StatementError

from paw_backend.db import Database

Row = Mapping[str, Any]

_STATEMENTS: dict[str, str] = {
    "users": (
        "SELECT id, login_name, system_role, status, passkey_required,"
        " created_at, updated_at FROM users ORDER BY id"
    ),
    "quotas": (
        "SELECT user_id, kind, metric, period, limit_value, created_at, updated_at"
        " FROM connection_quotas ORDER BY user_id, kind, metric, period"
    ),
    "projects": (
        "SELECT id, name, description, status, created_by, created_at, updated_at,"
        " deletion_started_at, deletion_scheduled_at, deleted_at"
        " FROM projects ORDER BY id"
    ),
    "members": (
        "SELECT project_id, user_id, role, status, invited_at, invite_expires_at,"
        " joined_at FROM project_members ORDER BY project_id, user_id"
    ),
    "repositories": (
        "SELECT id, project_id, name, default_branch, source, acl_allowed,"
        " created_by, created_at, updated_at FROM repositories ORDER BY id"
    ),
    "remotes": (
        "SELECT repository_id, project_id, url, created_at"
        " FROM repository_remotes ORDER BY repository_id, url"
    ),
    "memories": "SELECT id, created_at FROM memories ORDER BY id",
    "versions": (
        "SELECT id, memory_id, version_number, scope, owner_user_id, project_id,"
        " project_group_id, repo_id, memory_type, title, content, importance,"
        " pinned, status, confirmation_state, freshness_policy, verified_at,"
        " revalidate_after, revalidate_triggers, on_stale, expires_at, commit_sha,"
        " branch, stale_since, attributes, actor_type, actor_user_id,"
        " change_reason, created_at"
        " FROM memory_versions ORDER BY memory_id, version_number"
    ),
    "relations": (
        "SELECT id, from_version_id, to_version_id, relation_type, reason,"
        " created_at FROM memory_relations ORDER BY id"
    ),
    "sources": (
        "SELECT id, memory_version_id, source_type, source_ref, source_deleted_at,"
        " created_at FROM memory_sources ORDER BY id"
    ),
    "auth_policy": (
        "SELECT version, passkey_owner, passkey_admin, passkey_user,"
        " recommend_passkey_to_users, stepup_window_minutes, updated_at"
        " FROM auth_policy ORDER BY id"
    ),
    "connections": (
        "SELECT kind, status, enabled, created_at, updated_at"
        " FROM shared_connections ORDER BY kind"
    ),
    "tasks": (
        "SELECT id, project_id, created_by, title, state, wait_reason, attempt,"
        " retry_count, created_at, updated_at FROM tasks ORDER BY id"
    ),
    "task_repositories": (
        "SELECT r.task_id, r.repository_id, r.branch, r.head_commit,"
        " r.review_status, r.evaluation_result, r.pr_number, r.pr_url, r.pr_state"
        " FROM task_attempt_repositories r"
        " JOIN tasks t ON t.id = r.task_id AND t.attempt = r.attempt"
        " ORDER BY r.task_id, r.repository_id"
    ),
}


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """The rows of one snapshot, by the keys of ``_STATEMENTS``."""

    users: tuple[Row, ...] = ()
    quotas: tuple[Row, ...] = ()
    projects: tuple[Row, ...] = ()
    members: tuple[Row, ...] = ()
    repositories: tuple[Row, ...] = ()
    remotes: tuple[Row, ...] = ()
    memories: tuple[Row, ...] = ()
    versions: tuple[Row, ...] = ()
    relations: tuple[Row, ...] = ()
    sources: tuple[Row, ...] = ()
    auth_policy: tuple[Row, ...] = ()
    connections: tuple[Row, ...] = ()
    tasks: tuple[Row, ...] = ()
    task_repositories: tuple[Row, ...] = ()


class RecoveryDatabaseError(Exception):
    """Reading the snapshot failed. ``sqlstate`` only; never the driver's text."""

    def __init__(self, sqlstate: str | None) -> None:
        self.sqlstate = sqlstate
        super().__init__(
            "reading the recovery snapshot failed"
            + (f" (SQLSTATE {sqlstate})" if sqlstate else "")
        )


class RecoverySource:
    """``snapshot()``: every row the Recovery Repository holds, one snapshot."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def snapshot(self) -> RecoverySnapshot:
        failure: RecoveryDatabaseError | None = None
        rows: dict[str, tuple[Row, ...]] = {}
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                for key, statement in _STATEMENTS.items():
                    result = await session.execute(text(statement))
                    rows[key] = tuple(dict(row._mapping) for row in result)
        except StatementError as error:
            orig = error.orig if isinstance(error, DBAPIError) else None
            sqlstate = getattr(orig, "sqlstate", None)
            failure = RecoveryDatabaseError(
                sqlstate if isinstance(sqlstate, str) else None
            )
        if failure is not None:
            try:
                raise failure from None
            except RecoveryDatabaseError as clean:
                clean.__context__ = None
                raise
        return RecoverySnapshot(**rows)


__all__ = ["RecoveryDatabaseError", "RecoverySnapshot", "RecoverySource", "Row"]
