"""The Workspace schema version the Recovery Repository records and a restore checks.

The schema version is the Alembic revision: ``migration_head()`` is the head of
the migrations shipped next to this code (``apps/backend/migrations``), the
schema the backup's statements were written for; ``known_revisions()`` every
revision of this release's chain (a restore accepts a backup made by any of
them, Decision 0054 8). ``SchemaUnknownError`` when the scripts cannot be read.
"""

import functools
from pathlib import Path

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[2] / "migrations"


class SchemaUnknownError(Exception):
    """The migration scripts cannot be read (not deployed next to the code)."""

    def __init__(self) -> None:
        super().__init__("the migration scripts of this release cannot be read")


@functools.cache
def _chain() -> tuple[str, frozenset[str]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not (MIGRATIONS_DIRECTORY / "env.py").is_file():
        raise SchemaUnknownError()
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
    try:
        scripts = ScriptDirectory.from_config(config)
        heads = scripts.get_heads()
        if len(heads) != 1:
            raise SchemaUnknownError()
        revisions = frozenset(
            script.revision
            for script in scripts.walk_revisions(base="base", head=heads[0])
        )
    except SchemaUnknownError:
        raise
    except Exception:
        raise SchemaUnknownError() from None
    return heads[0], revisions


def migration_head() -> str:
    return _chain()[0]


def known_revisions() -> frozenset[str]:
    return _chain()[1]


__all__ = [
    "MIGRATIONS_DIRECTORY",
    "SchemaUnknownError",
    "known_revisions",
    "migration_head",
]
