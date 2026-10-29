"""Fakes of ``pg_dump`` / ``pg_restore`` for the restore point tests (Issue #54).

The CI runner has no PostgreSQL client of the server's version, so the tests use
two small executables with the same interface (connection in ``PG*`` variables,
``--file=`` / ``--dbname=`` and the dump's path as arguments) that read and write
a real database with psycopg: the "dump" is the database's revision and its
tables' columns (and its extensions); the "restore" creates those tables
(empty) and ``alembic_version`` with the revision, and creates and comments on
the extensions as a real dump does (which needs a superuser for ``vector``)
unless ``--use-list`` left them out. ``--list`` writes a TOC like the real
one. Each call is appended to ``calls.jsonl``
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


def log(name, can_fail=True):
    with (HERE / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps({
            "tool": name,
            "argv": sys.argv[1:],
            "pg": sorted(k for k in os.environ if k.startswith("PG")),
        }) + "\\n")
    if can_fail and (HERE / f"{name}.fail").exists():
        sys.exit(1)
"""

_DUMP = """
log("pg_dump")
target = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--file="))
if (HERE / "pg_dump.fail_after_writing").exists():
    Path(target).write_text("part of a dump")  # a disk-full error, a kill
    sys.exit(1)
with connect() as connection:
    (revision,) = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    extensions = [
        name for (name,) in connection.execute(
            "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1")
    ]
    tables = {}
    for table, column, kind in connection.execute(
        # format_type: an extension's type too (vector(3)).
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid WHERE n.nspname = 'public'"
        " AND c.relkind = 'r' AND c.relname <> 'alembic_version'"
        " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, a.attnum"):
        tables.setdefault(table, []).append([column, kind])
Path(target).write_text(json.dumps(
    {"revision": revision, "tables": tables, "extensions": extensions}
))
"""

_RESTORE = """
# pg_restore.fail fails the restore, not the listing of the TOC.
log("pg_restore", can_fail="--list" not in sys.argv)
dump = json.loads(Path(sys.argv[-1]).read_text())
extensions = dump.get("extensions", [])
if "--list" in sys.argv:
    # The TOC as pg_restore --list prints it (the entries this fake knows).
    target = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--file="))
    lines = [";", "; Archive created by the fake pg_dump", ";"]
    entry = 1
    for name in extensions:
        lines.append(f"{entry}; 3079 {16000 + entry} EXTENSION - {name} ")
        lines.append(f"{entry + 1}; 0 0 COMMENT - EXTENSION {name} ")
        entry += 2
    for table in ["alembic_version", *dump["tables"]]:
        lines.append(f"{entry}; 1259 {16000 + entry} TABLE public {table} owner")
        entry += 1
    Path(target).write_text("\\n".join(lines) + "\\n")
    sys.exit(0)
selected = next(
    (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--use-list=")), None
)
if selected is not None:
    # The entries left in the list (a line starting with ";" is left out).
    listed = set()
    for line in Path(selected).read_text().splitlines():
        if line.startswith(";"):
            continue
        words = line.split(";", 1)[1].split()
        listed.add(tuple(words[2:5]))
    extensions = [n for n in extensions if ("EXTENSION", "-", n) in listed]
database = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--dbname="))
with connect(database) as connection:
    for name in extensions:
        # As pg_dump writes them: only a superuser creates vector, only the
        # extension's owner comments on it.
        try:
            connection.execute(f'CREATE EXTENSION IF NOT EXISTS "{name}"')
            connection.execute(f"COMMENT ON EXTENSION \\"{name}\\" IS 'restored'")
        except psycopg.Error as error:
            print(error, file=sys.stderr)
            sys.exit(1)
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
