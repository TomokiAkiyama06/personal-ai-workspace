"""The manual release of a crashed process's repository write (issue #129).

Revision ID: 0129
Revises: 0133
Create Date: 2026-09-28

Decision 0049 (Proposed): a person may release, by hand, the repository write
reservation (``task_repository_writes``) of a process that crashed, instead of
waiting for it to expire (Decision 0035, section 5). The release uses the columns
revision 0085 already has (``released_at``, and the attempt state it marks as
written), so the only schema change is the history:

* ``task_events.command`` accepts ``release_repository_write``.

No privilege changes: the application role already selects and inserts
``task_events``, updates ``task_repository_writes.released_at`` and the attempt
state columns, and reads (and locks ``FOR SHARE``) ``queue_entries``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0129"
down_revision: str | Sequence[str] | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_COMMANDS = tuple(
    "create start wait unblock begin_evaluation complete fail "
    "pause resume cancel retry restart stop_now change_working_set".split()
)
NEW_COMMANDS = (*OLD_COMMANDS, "release_repository_write")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


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
    _replace_command_check(NEW_COMMANDS, validate=True)


def downgrade() -> None:
    # ``task_events`` is append-only (a trigger refuses UPDATE and DELETE), so the
    # history of manual releases stays; the old list is then not validated
    # against it (NOT VALID) and still holds for every new row.
    _replace_command_check(OLD_COMMANDS, validate=False)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM task_events WHERE command = 'release_repository_write'
            ) THEN
                ALTER TABLE task_events
                    VALIDATE CONSTRAINT ck_task_events_command_valid;
            END IF;
        END
        $$
        """
    )
