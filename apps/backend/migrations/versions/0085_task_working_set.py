"""The Working Set of a task (issue #85, Decisions 0014 and 0030).

Revision ID: 0085
Revises: 0034
Create Date: 2026-09-28

* ``task_repositories``: the repositories of a task and their roles
  (``referenced`` / ``working`` / ``target``), each with its own starting commit
  (#85 constraint 1), who added it or last changed its role, and when it left the
  Working Set (``removed_at``: nothing of a task is deleted). Kept across Retry and
  Restart (Decision 0030, section 1). ``repository_id`` has no foreign key to
  ``repositories``: the history of a task outlives a purged registration.
* ``task_attempt_repositories``: the branch / worktree / review / evaluation / pull
  request state of one repository in one attempt, with the strongest role it held
  and whether a repository write on it was allowed (section 5). It references its
  attempt and its Working Set row.
* The per-attempt state moves there: ``task_attempts`` loses ``branch``,
  ``worktree_path``, ``head_commit``, ``review_status``, ``evaluation_result`` and
  ``pr_*`` (one model for Single- and Multi-Repo tasks, section 2), and ``tasks``
  loses ``starting_commit`` (a commit per repository instead). An existing
  attempt names no repository, so its state cannot be attributed to one and the
  upgrade does not guess: **it first copies every attempt's state, with its
  task's starting commit, to ``task_attempt_state_archive``** (nothing is lost;
  the application has no privilege on it). Such a task has an empty Working Set:
  it cannot run (Start, Resume, Unblock need a ``target``), complete or touch a
  repository until it is given one (``TaskService.change_working_set``). The
  downgrade puts the columns back with the state of an attempt that has exactly
  one repository, and the archived state of an attempt that has none (then drops
  the archive).
* ``task_repository_writes``: a repository write (or execution) the Tool Broker
  admitted and whose executor may still be running (Codex review of #85), with
  the run it was admitted for: a repository with a live one (not released, not
  expired) is not downgraded or removed, and its task does not begin evaluation
  or complete. Released by ``released_at``; nothing is deleted.
* ``task_events.command`` accepts ``change_working_set``.

The application role gets INSERT and SELECT on the three tables and UPDATE on the
columns ``TaskService`` changes (never the identity of a row); DELETE nowhere.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role, grant_app_privileges

revision: str = "0085"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("referenced", "working", "target")
ACTOR_KINDS = ("user", "system", "policy")
REVIEW_STATUSES = ("not_started", "in_review", "approved", "changes_requested")
EVALUATION_RESULTS = ("not_run", "passed", "failed")
PR_STATES = ("draft", "open", "merged", "closed")
OLD_COMMANDS = tuple(
    "create start wait unblock begin_evaluation complete fail "
    "pause resume cancel retry restart stop_now".split()
)
NEW_COMMANDS = (*OLD_COMMANDS, "change_working_set")
# What the columns below held before this revision (see the module docstring).
ARCHIVE = "task_attempt_state_archive"
# The archive is for an operator (and the downgrade): the application never reads
# or writes it, so it gets no privilege (``REVOKE ALL ... FROM PUBLIC`` only).
NO_APP_GRANTS = {
    "task_attempt_state_archive": (
        "archive of the pre-0085 attempt state, never used by the application"
    ),
}
# The columns of revision 0032 that move to ``task_attempt_repositories``.
ATTEMPT_STATE_COLUMNS = (
    "branch",
    "worktree_path",
    "head_commit",
    "review_status",
    "evaluation_result",
    "pr_number",
    "pr_url",
    "pr_state",
)


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _in(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({_listed(values)})", name=op.f(name))


def _state_columns(*, filled: bool = False) -> list[sa.Column]:
    """The state columns; ``filled`` gives the two NOT NULL ones a default so
    that they can be added to a table that has rows."""

    def default(value: str) -> sa.TextClause | None:
        return sa.text(f"'{value}'") if filled else None

    return [
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("worktree_path", sa.String(length=1024), nullable=True),
        sa.Column("head_commit", sa.String(length=64), nullable=True),
        sa.Column(
            "review_status",
            sa.String(length=24),
            nullable=False,
            server_default=default("not_started"),
        ),
        sa.Column(
            "evaluation_result",
            sa.String(length=24),
            nullable=False,
            server_default=default("not_run"),
        ),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=2048), nullable=True),
        sa.Column("pr_state", sa.String(length=24), nullable=True),
    ]


def _replace_command_check(values: Sequence[str], *, validate: bool) -> None:
    op.drop_constraint(
        op.f("ck_task_events_command_valid"), "task_events", type_="check"
    )
    not_valid = "" if validate else " NOT VALID"
    op.execute(
        "ALTER TABLE task_events ADD CONSTRAINT ck_task_events_command_valid "
        f"CHECK (command IN ({_listed(values)})){not_valid}"
    )


def upgrade() -> None:
    op.create_table(
        "task_repositories",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("starting_commit", sa.String(length=64), nullable=True),
        sa.Column("added_by_kind", sa.String(length=24), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "task_id", "repository_id", name=op.f("pk_task_repositories")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_repositories_task_id_tasks")
        ),
        _in("role", ROLES, "ck_task_repositories_role_valid"),
        _in("added_by_kind", ACTOR_KINDS, "ck_task_repositories_added_by_kind_valid"),
        sa.CheckConstraint(
            "(added_by_kind = 'user') = (added_by IS NOT NULL)",
            name=op.f("ck_task_repositories_added_by_matches_kind"),
        ),
    )
    grant_app_privileges(
        op,
        "task_repositories",
        insert=True,
        update_columns=(
            "role",
            "starting_commit",
            "added_by_kind",
            "added_by",
            "added_at",
            "updated_at",
            "removed_at",
        ),
    )

    op.create_table(
        "task_attempt_repositories",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        *_state_columns(),
        sa.Column("strongest_role", sa.String(length=24), nullable=False),
        sa.Column("modified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_attempt_repositories")),
        sa.ForeignKeyConstraint(
            ["task_id", "attempt"],
            ["task_attempts.task_id", "task_attempts.number"],
            name=op.f("fk_task_attempt_repositories_task_id_task_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "repository_id"],
            ["task_repositories.task_id", "task_repositories.repository_id"],
            name=op.f("fk_task_attempt_repositories_task_id_task_repositories"),
        ),
        sa.UniqueConstraint(
            "task_id",
            "attempt",
            "repository_id",
            name=op.f("uq_task_attempt_repositories_task_id"),
        ),
        _in(
            "review_status",
            REVIEW_STATUSES,
            "ck_task_attempt_repositories_review_status_valid",
        ),
        _in(
            "evaluation_result",
            EVALUATION_RESULTS,
            "ck_task_attempt_repositories_evaluation_result_valid",
        ),
        _in("pr_state", PR_STATES, "ck_task_attempt_repositories_pr_state_valid"),
        _in(
            "strongest_role", ROLES, "ck_task_attempt_repositories_strongest_role_valid"
        ),
        sa.CheckConstraint(
            "(pr_number IS NULL) = (pr_url IS NULL)"
            " AND (pr_number IS NULL) = (pr_state IS NULL)",
            name=op.f("ck_task_attempt_repositories_pull_request_complete"),
        ),
    )
    grant_app_privileges(
        op,
        "task_attempt_repositories",
        insert=True,
        update_columns=(
            *ATTEMPT_STATE_COLUMNS,
            "strongest_role",
            "modified",
            "updated_at",
        ),
    )

    op.create_table(
        "task_repository_writes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "id", "repository_id", name=op.f("pk_task_repository_writes")
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "attempt"],
            ["task_attempts.task_id", "task_attempts.number"],
            name=op.f("fk_task_repository_writes_task_id_task_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "repository_id"],
            ["task_repositories.task_id", "task_repositories.repository_id"],
            name=op.f("fk_task_repository_writes_task_id_task_repositories"),
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name=op.f("ck_task_repository_writes_retry_count_not_negative"),
        ),
        sa.CheckConstraint(
            "expires_at > admitted_at",
            name=op.f("ck_task_repository_writes_expires_after_admission"),
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= admitted_at",
            name=op.f("ck_task_repository_writes_released_after_admission"),
        ),
    )
    op.create_index(
        op.f("ix_task_repository_writes_task_id"),
        "task_repository_writes",
        ["task_id", "repository_id"],
    )
    grant_app_privileges(
        op, "task_repository_writes", insert=True, update_columns=("released_at",)
    )

    # What the columns hold is kept before they are dropped (Claude review of #85:
    # no data loss). Plain text, no CHECK: it keeps whatever was there.
    op.create_table(
        "task_attempt_state_archive",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        *_state_columns(),
        sa.Column("starting_commit", sa.String(length=64), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "task_id", "number", name=op.f("pk_task_attempt_state_archive")
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "number"],
            ["task_attempts.task_id", "task_attempts.number"],
            name=op.f("fk_task_attempt_state_archive_task_id_task_attempts"),
        ),
    )
    # Nothing of the application reads or writes it.
    op.execute(f"REVOKE ALL ON {ARCHIVE} FROM PUBLIC")
    columns = ", ".join(ATTEMPT_STATE_COLUMNS)
    selected = ", ".join(f"a.{c}" for c in ATTEMPT_STATE_COLUMNS)
    op.execute(
        f"""
        INSERT INTO {ARCHIVE}
            (task_id, number, {columns}, starting_commit, archived_at)
        SELECT a.task_id, a.number, {selected}, t.starting_commit, now()
        FROM task_attempts AS a JOIN tasks AS t ON t.id = a.task_id
        """
    )

    # Dropping a column drops the constraints that name it (the value checks and
    # ``pull_request_complete``) and the column privileges on it.
    for column in ATTEMPT_STATE_COLUMNS:
        op.drop_column("task_attempts", column)
    op.drop_column("tasks", "starting_commit")
    # Nothing updates an attempt row any more (its state moved): least privilege.
    _attempt_updates("REVOKE UPDATE (updated_at) ON task_attempts FROM {role}")
    _replace_command_check(NEW_COMMANDS, validate=True)


def _attempt_updates(statement: str) -> None:
    """Run ``statement`` for the application role (none configured: nothing)."""
    role = configured_app_role()
    if role is None:
        return
    quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
    op.execute(statement.format(role=quoted))


def downgrade() -> None:
    # ``task_events`` is append-only (a trigger refuses UPDATE and DELETE), so the
    # history of Working Set changes stays; the old list is then not validated
    # against it (NOT VALID) and still holds for every new row.
    _replace_command_check(OLD_COMMANDS, validate=False)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM task_events WHERE command = 'change_working_set'
            ) THEN
                ALTER TABLE task_events
                    VALIDATE CONSTRAINT ck_task_events_command_valid;
            END IF;
        END
        $$
        """
    )

    op.add_column("tasks", sa.Column("starting_commit", sa.String(64), nullable=True))
    for column in _state_columns(filled=True):
        op.add_column("task_attempts", column)
    # The state of an attempt that has exactly one repository, and the starting
    # commit of a task whose Working Set holds exactly one repository.
    op.execute(
        f"""
        UPDATE task_attempts AS a SET
            {", ".join(f"{c} = r.{c}" for c in ATTEMPT_STATE_COLUMNS)}
        FROM task_attempt_repositories AS r
        WHERE r.task_id = a.task_id AND r.attempt = a.number
          AND (SELECT count(*) FROM task_attempt_repositories AS o
               WHERE o.task_id = a.task_id AND o.attempt = a.number) = 1
        """
    )
    op.execute(
        """
        UPDATE tasks AS t SET starting_commit = r.starting_commit
        FROM task_repositories AS r
        WHERE r.task_id = t.id
          AND (SELECT count(*) FROM task_repositories AS o
               WHERE o.task_id = t.id) = 1
        """
    )
    # An attempt with no repository (one of before this revision): its archived
    # state, and its task's starting commit when the task has no repository.
    op.execute(
        f"""
        UPDATE task_attempts AS a SET
            {", ".join(f"{c} = s.{c}" for c in ATTEMPT_STATE_COLUMNS)}
        FROM {ARCHIVE} AS s
        WHERE s.task_id = a.task_id AND s.number = a.number
          AND NOT EXISTS (SELECT 1 FROM task_attempt_repositories AS o
                          WHERE o.task_id = a.task_id AND o.attempt = a.number)
        """
    )
    op.execute(
        f"""
        UPDATE tasks AS t SET starting_commit = s.starting_commit
        FROM {ARCHIVE} AS s
        WHERE s.task_id = t.id AND s.number = 1
          AND NOT EXISTS (SELECT 1 FROM task_repositories AS o
                          WHERE o.task_id = t.id)
        """
    )
    op.drop_table(ARCHIVE)
    for column in ("review_status", "evaluation_result"):
        op.alter_column("task_attempts", column, server_default=None)
    op.create_check_constraint(
        op.f("ck_task_attempts_review_status_valid"),
        "task_attempts",
        f"review_status IN ({_listed(REVIEW_STATUSES)})",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_evaluation_result_valid"),
        "task_attempts",
        f"evaluation_result IN ({_listed(EVALUATION_RESULTS)})",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_pr_state_valid"),
        "task_attempts",
        f"pr_state IN ({_listed(PR_STATES)})",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_pull_request_complete"),
        "task_attempts",
        "(pr_number IS NULL) = (pr_url IS NULL)"
        " AND (pr_number IS NULL) = (pr_state IS NULL)",
    )
    _attempt_updates(
        f"GRANT UPDATE ({', '.join((*ATTEMPT_STATE_COLUMNS, 'updated_at'))}) "
        "ON task_attempts TO {role}"
    )
    op.drop_table("task_repository_writes")
    op.drop_table("task_attempt_repositories")
    op.drop_table("task_repositories")
