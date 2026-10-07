"""Fakes of ``pg_dump`` / ``pg_restore`` for the restore point tests (Issue #54).

The CI runner has no PostgreSQL client of the server's version, so the tests use
two small executables with the same interface (connection in ``PG*`` variables,
``--file=`` / ``--dbname=`` and the dump's path as arguments) that read and write
a real database with psycopg: the "dump" is the database's revision and its
tables' columns; the "restore" creates those tables (empty) and
``alembic_version`` with the revision. Each call is appended to ``calls.jsonl``
next to them (its arguments and the names of its ``PG*`` variables, never their
values), so a test can see that no credential was on the command line.
"""

import os
import sys
import textwrap
from pathlib import Path

_COMMON = """
import json, os, sys
from pathlib import Path
import psycopg

HERE = Path(__file__).resolve().parent


def connect(dbname=None):
    return psycopg.connect(
        host=os.environ.get("PGHOST"),
        port=os.environ.get("PGPORT"),
        user=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        dbname=dbname or os.environ.get("PGDATABASE"),
        autocommit=True,
    )


def log(name):
    with (HERE / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps({
            "tool": name,
            "argv": sys.argv[1:],
            "pg": sorted(k for k in os.environ if k.startswith("PG")),
        }) + "\\n")
    if (HERE / f"{name}.fail").exists():
        sys.exit(1)
"""

_DUMP = """
log("pg_dump")
target = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--file="))
with connect() as connection:
    (revision,) = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    tables = {}
    for table, column, kind in connection.execute(
        "SELECT t.tablename, c.column_name, c.data_type FROM pg_tables t"
        " JOIN information_schema.columns c ON c.table_schema = t.schemaname"
        " AND c.table_name = t.tablename WHERE t.schemaname = 'public'"
        " AND t.tablename <> 'alembic_version' ORDER BY 1, c.ordinal_position"):
        tables.setdefault(table, []).append([column, kind])
Path(target).write_text(json.dumps({"revision": revision, "tables": tables}))
"""

_RESTORE = """
log("pg_restore")
database = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--dbname="))
dump = json.loads(Path(sys.argv[-1]).read_text())
with connect(database) as connection:
    connection.execute(
        "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"
    )
    connection.execute("INSERT INTO alembic_version VALUES (%s)", (dump["revision"],))
    for table, columns in dump["tables"].items():
        listed = ", ".join(f'"{name}" {kind}' for name, kind in columns)
        connection.execute(f'CREATE TABLE "{table}" ({listed})')
"""


def write_fake_pg_tools(directory: Path) -> tuple[str, str]:
    """``(pg_dump, pg_restore)``: the paths of the two fakes in ``directory``."""
    paths = []
    for name, body in (("pg_dump", _DUMP), ("pg_restore", _RESTORE)):
        path = directory / name
        path.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(_COMMON) + textwrap.dedent(body)
        )
        os.chmod(path, 0o755)
        paths.append(str(path))
    return paths[0], paths[1]
