"""The database restore point of an update, its verification and its restore
(Issue #54, Decision 0079 5).

REQUIREMENTS.md ("Pre-update checks", FIXED) requires, before the first schema
or data change, a consistent database backup with a restore procedure verified
in an isolated environment, kept outside the Recovery Repository; a failed
backup, too little space or a failed verification means the migration does not
start. Here:

* **create**: ``pg_dump --format=custom`` of the workspace database (as the
  migration role, the table owner) into ``<dir>/<label>.dump``, written to a
  temporary name and renamed when complete, with ``<label>.json`` beside it:
  the schema revision the database had, the size and the SHA-256 of the dump.
  ``dir`` is mode 0700 and must not be inside the Recovery Repository (no dump
  goes to Git). Taken while every database writer is stopped, the dump is
  consistent with the state the migration starts from.
* **verify**: a scratch database (``<db>_verify_<hex>``) is created with the
  admin connection, the dump is restored into it with ``pg_restore
  --single-transaction --exit-on-error``, its ``alembic_version`` must be the
  recorded revision and it must have tables; then the scratch database is
  dropped. The result is written into the ``.json`` (``verified``).
* **restore**: only a verified point whose dump still has its checksum, and
  only when no user was deleted (``user_status_changes``) after the point was
  taken (the database's clock). It is
  restored into a new database (``<db>_restore_<stamp>``) and checked as above;
  then the workspace database is renamed to ``<db>_replaced_<stamp>`` (kept for
  the operator to inspect and drop) and the new one takes its name. Restoring
  into a new database also removes what a failed migration created, which
  ``pg_restore --clean`` would leave behind.

**Extensions** (Issue #312): the dump creates the extensions the database uses
(``vector``, which only a superuser may create), and the restoring role is not
a superuser. With a **restore template** (``PAW_DEPLOY_RESTORE_TEMPLATE``: a
database a superuser made once, ``IS_TEMPLATE``, with those extensions and
nothing else) the scratch / restore databases are copies of it, and
``pg_restore --use-list`` leaves out the dump's ``EXTENSION`` and ``COMMENT -
EXTENSION`` entries (the template has them; commenting on an extension needs
its owner). Before the restore the copy must have every extension the dump
creates, no other (``plpgsql`` aside) and no table; after it, every extension
the dump creates must be there. Without a template the scratch database is a
copy of ``template0`` and the dump is restored whole, which needs a role that
may create its extensions: when it may not, the restore is refused
(``extension_needs_restore_template``) before ``pg_restore`` runs.

Credentials go to ``pg_dump`` / ``pg_restore`` in their environment (``PGHOST``,
``PGUSER``, ``PGPASSWORD``, ...), never on the command line, and nothing printed
contains them. ``pg_dump`` / ``pg_restore`` must be of the server's major version
or newer.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

FORMAT_VERSION = 1
LABEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
TEMPLATE_PATTERN = r"[A-Za-z_][A-Za-z0-9_]{0,62}"
# In every database made from template0 (CREATE DATABASE copies it), never in
# a dump.
_BUILT_IN_EXTENSIONS = frozenset({"plpgsql"})
# A TOC entry of ``pg_restore --list``: "<id>; <catalog oid> <oid> <what>".
_TOC_ENTRY = re.compile(r"\d+; +\d+ +\d+ +(.*)")
_IDENTIFIER_MAX = 63
# The URL query keys passed on to libpq's environment.
_QUERY_ENVIRONMENT = {
    "host": "PGHOST",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
}

Runner = Callable[..., subprocess.CompletedProcess]


class RestorePointError(Exception):
    """A restore point could not be created, verified or restored. ``code`` is a
    closed word for the audit row and the exit message; there is no message
    with paths or URLs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RestorePoint:
    label: str
    database: str
    revision: str
    created_at: str
    size: int
    sha256: str
    verified: Mapping[str, object] | None
    metadata_path: Path

    @property
    def dump_path(self) -> Path:
        return self.metadata_path.with_suffix(".dump")

    def as_json(self) -> dict[str, object]:
        return {
            "format": FORMAT_VERSION,
            "label": self.label,
            "database": self.database,
            "revision": self.revision,
            "created_at": self.created_at,
            "size": self.size,
            "sha256": self.sha256,
            "verified": dict(self.verified) if self.verified is not None else None,
        }


def pg_environment(url: URL, *, database: str | None = None) -> dict[str, str]:
    """libpq's environment for ``url`` (``database`` instead of its own)."""
    environment: dict[str, str] = {}
    if url.host:
        environment["PGHOST"] = url.host
    if url.port:
        environment["PGPORT"] = str(url.port)
    if url.username:
        environment["PGUSER"] = url.username
    if url.password is not None:
        environment["PGPASSWORD"] = str(url.password)
    name = database or url.database
    if name:
        environment["PGDATABASE"] = name
    for key, value in url.query.items():
        variable = _QUERY_ENVIRONMENT.get(key)
        if variable is None:
            continue
        environment[variable] = value if isinstance(value, str) else value[0]
    return environment


def _sync_url(url: URL, database: str | None = None) -> URL:
    url = url.set(drivername="postgresql+psycopg")
    return url.set(database=database) if database is not None else url


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _scratch_name(base: str, kind: str) -> str:
    suffix = f"_{kind}_{datetime.now(UTC):%Y%m%d%H%M%S}_{secrets.token_hex(3)}"
    return base[: _IDENTIFIER_MAX - len(suffix)] + suffix


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class RestorePoints:
    """Restore points in one directory. ``runner`` runs ``pg_dump`` /
    ``pg_restore`` (``subprocess.run``; tests pass fakes), ``engine`` makes the
    SQL connections (``create_engine``)."""

    def __init__(
        self,
        directory: Path,
        *,
        recovery_directory: Path | None = None,
        pg_dump: str = "pg_dump",
        pg_restore: str = "pg_restore",
        runner: Runner = subprocess.run,
        engine: Callable[..., Engine] = create_engine,
        timeout_seconds: float = 6 * 3600,
        template: str | None = None,
    ) -> None:
        directory = Path(directory)
        if not directory.is_absolute():
            raise RestorePointError("directory_not_absolute")
        if template is not None and not re.fullmatch(TEMPLATE_PATTERN, template):
            raise RestorePointError("invalid_restore_template")
        self._template = template
        self._directory = directory
        self._recovery = (
            Path(recovery_directory) if recovery_directory is not None else None
        )
        self._pg_dump = pg_dump
        self._pg_restore = pg_restore
        self._runner = runner
        self._engine = engine
        self._timeout = timeout_seconds

    # -- the directory ---------------------------------------------------------

    def _checked_directory(self, *, create: bool) -> Path:
        directory = self._directory
        resolved = directory.resolve()
        if self._recovery is not None:
            recovery = self._recovery.resolve()
            if _inside(resolved, recovery) or _inside(recovery, resolved):
                raise RestorePointError("directory_in_recovery_repository")
        if not directory.exists():
            if not create:
                raise RestorePointError("directory_missing")
            directory.mkdir(mode=0o700, parents=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise RestorePointError("directory_not_a_directory")
        if info.st_mode & 0o077:
            raise RestorePointError("directory_not_private")
        return directory

    def free_bytes(self) -> int:
        directory = self._directory if self._directory.exists() else Path("/")
        usage = os.statvfs(directory)
        return usage.f_bavail * usage.f_frsize

    # -- create ----------------------------------------------------------------

    def create(self, url: URL, label: str) -> RestorePoint:
        if not re.fullmatch(LABEL_PATTERN, label):
            raise RestorePointError("invalid_label")
        directory = self._checked_directory(create=True)
        metadata = directory / f"{label}.json"
        dump = directory / f"{label}.dump"
        if metadata.exists() or dump.exists():
            raise RestorePointError("label_exists")
        if not url.database:
            raise RestorePointError("no_database")
        revision = self._revision(_sync_url(url))
        # The database's clock: what ``restore`` compares the deletions with.
        taken_at = self._scalar(_sync_url(url), "SELECT now()")
        partial = directory / f".{label}.dump.partial"
        try:
            self._run(
                [
                    self._pg_dump,
                    "--format=custom",
                    "--no-password",
                    f"--file={partial}",
                ],
                pg_environment(url),
                "dump_failed",
            )
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RestorePointError("dump_failed")
            os.chmod(partial, 0o600)
            with partial.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(partial, dump)
            point = RestorePoint(
                label=label,
                database=url.database,
                revision=revision,
                created_at=taken_at.isoformat(),
                size=dump.stat().st_size,
                sha256=_sha256(dump),
                verified=None,
                metadata_path=metadata,
            )
            _write_private(metadata, json.dumps(point.as_json(), indent=2) + "\n")
        except BaseException:
            # A failed or cut-off dump holds personal data: nothing of it stays
            # (prune only sees finished points).
            for leftover in (partial, dump, metadata):
                with contextlib.suppress(FileNotFoundError):
                    leftover.unlink()
            raise
        return point

    # -- load ------------------------------------------------------------------

    def load(self, label: str) -> RestorePoint:
        if not re.fullmatch(LABEL_PATTERN, label):
            raise RestorePointError("invalid_label")
        directory = self._checked_directory(create=False)
        metadata = directory / f"{label}.json"
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RestorePointError("not_found") from None
        except (OSError, ValueError):
            raise RestorePointError("metadata_unreadable") from None
        if not isinstance(data, dict) or data.get("format") != FORMAT_VERSION:
            raise RestorePointError("metadata_unreadable")
        try:
            point = RestorePoint(
                label=str(data["label"]),
                database=str(data["database"]),
                revision=str(data["revision"]),
                created_at=str(data["created_at"]),
                size=int(data["size"]),
                sha256=str(data["sha256"]),
                verified=data.get("verified"),
                metadata_path=metadata,
            )
        except (KeyError, TypeError, ValueError):
            raise RestorePointError("metadata_unreadable") from None
        if point.label != label:
            raise RestorePointError("metadata_unreadable")
        return point

    def _check_dump(self, point: RestorePoint) -> None:
        dump = point.dump_path
        if not dump.is_file():
            raise RestorePointError("dump_missing")
        if dump.stat().st_size != point.size or _sha256(dump) != point.sha256:
            raise RestorePointError("checksum_mismatch")

    # -- verify ----------------------------------------------------------------

    def verify(self, point: RestorePoint, url: URL, admin_url: URL) -> RestorePoint:
        """Restore into a scratch database, check it, drop it; record the
        result in the metadata (``RestorePointError`` when it failed)."""
        self._check_dump(point)
        scratch = _scratch_name(point.database, "verify")
        owner = self._create_like(admin_url, point.database, scratch)
        try:
            tables = self._restore_into(point, url, scratch)
        finally:
            self._drop(admin_url, scratch)
        verified = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "revision": point.revision,
            "tables": tables,
            "owner": owner,
        }
        point = replace(point, verified=verified)
        _write_private(
            point.metadata_path, json.dumps(point.as_json(), indent=2) + "\n"
        )
        return point

    # -- restore ---------------------------------------------------------------

    def restore(self, point: RestorePoint, url: URL, admin_url: URL) -> str:
        """Put the point back as the workspace database; the name the replaced
        database was given."""
        if not point.verified:
            raise RestorePointError("not_verified")
        if url.database != point.database:
            raise RestorePointError("other_database")
        self._check_dump(point)
        # A user deleted since the point was taken would be back after the
        # restore (REQUIREMENTS: restored backups must not resurrect erased
        # data). Within an update the writers are stopped from the point to the
        # restore, so there are none; a later manual rollback is refused.
        history = self._scalar(
            _sync_url(url), "SELECT to_regclass('user_status_changes') IS NOT NULL"
        )
        if history and self._scalar(
            _sync_url(url),
            "SELECT count(*) FROM user_status_changes WHERE new_status IN"
            " ('pending_deletion', 'deleted') AND recorded_at > :since",
            since=datetime.fromisoformat(point.created_at),
        ):
            raise RestorePointError("users_deleted_since_the_point")
        incoming = _scratch_name(point.database, "restore")
        self._create_like(admin_url, point.database, incoming)
        try:
            self._restore_into(point, url, incoming)
        except BaseException:
            self._drop(admin_url, incoming)
            raise
        replaced = _scratch_name(point.database, "replaced")
        with self._admin(admin_url) as connection:
            try:
                connection.execute(
                    text(
                        f"ALTER DATABASE {_quote(point.database)} "
                        f"RENAME TO {_quote(replaced)}"
                    )
                )
            except Exception:
                # Still in use (a writer was not stopped) or not allowed: the
                # workspace database is untouched; the restored copy goes.
                connection.execute(
                    text(f"DROP DATABASE IF EXISTS {_quote(incoming)} WITH (FORCE)")
                )
                raise RestorePointError("database_in_use_or_not_allowed") from None
            try:
                connection.execute(
                    text(
                        f"ALTER DATABASE {_quote(incoming)} "
                        f"RENAME TO {_quote(point.database)}"
                    )
                )
            except Exception:
                # The workspace database must not stay without its name: put
                # it back as it was and drop the restored copy.
                try:
                    connection.execute(
                        text(
                            f"ALTER DATABASE {_quote(replaced)} "
                            f"RENAME TO {_quote(point.database)}"
                        )
                    )
                except Exception:
                    # Both the original (under ``replaced``) and the restored
                    # copy are kept for the operator.
                    raise RestorePointError("rename_failed_not_put_back") from None
                connection.execute(
                    text(f"DROP DATABASE IF EXISTS {_quote(incoming)} WITH (FORCE)")
                )
                raise RestorePointError("rename_failed") from None
        return replaced

    # -- helpers ---------------------------------------------------------------

    def _run(self, argv: list[str], environment: dict[str, str], code: str) -> None:
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("PG")
        }
        env.update(environment)
        try:
            completed = self._runner(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise RestorePointError(code) from None
        if completed.returncode != 0:
            raise RestorePointError(code)

    def _revision(self, url: URL) -> str:
        engine = self._engine(url)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).all()
        except Exception:
            raise RestorePointError("revision_unreadable") from None
        finally:
            engine.dispose()
        if len(rows) != 1:
            raise RestorePointError("revision_unreadable")
        return str(rows[0][0])

    def _scalar(
        self, url: URL, sql: str, *, code: str = "database_unreadable", **parameters
    ):
        engine = self._engine(url)
        try:
            with engine.connect() as connection:
                return connection.execute(text(sql), parameters).scalar_one()
        except Exception:
            raise RestorePointError(code) from None
        finally:
            engine.dispose()

    def _extensions(self, url: URL, code: str) -> set[str]:
        engine = self._engine(url)
        try:
            with engine.connect() as connection:
                rows = connection.execute(text("SELECT extname FROM pg_extension"))
                return {str(row[0]) for row in rows}
        except Exception:
            raise RestorePointError(code) from None
        finally:
            engine.dispose()

    def _admin(self, admin_url: URL):
        engine = self._engine(_sync_url(admin_url), isolation_level="AUTOCOMMIT")
        return _Connection(engine)

    def _create_like(self, admin_url: URL, source: str, name: str) -> str:
        """Create ``name`` with the owner and encoding of ``source`` (a copy of
        the restore template, else of ``template0``); its owner."""
        template = self._template or "template0"
        if template == source:
            raise RestorePointError("restore_template_is_the_database")
        try:
            with self._admin(admin_url) as connection:
                if self._template is not None:
                    # Not connected to: a connection would stop the copy.
                    marked = connection.execute(
                        text(
                            "SELECT datistemplate FROM pg_database"
                            " WHERE datname = :name"
                        ),
                        {"name": template},
                    ).scalar_one_or_none()
                    if marked is None:
                        raise RestorePointError("restore_template_missing")
                    if not marked:
                        raise RestorePointError("restore_template_not_a_template")
                row = connection.execute(
                    text(
                        "SELECT pg_get_userbyid(datdba) AS owner,"
                        " pg_encoding_to_char(encoding) AS encoding"
                        " FROM pg_database WHERE datname = :name"
                    ),
                    {"name": source},
                ).one_or_none()
                if row is None:
                    raise RestorePointError("source_database_missing")
                connection.execute(
                    text(
                        f"CREATE DATABASE {_quote(name)} TEMPLATE {_quote(template)}"
                        f" OWNER {_quote(row.owner)}"
                        f" ENCODING '{row.encoding}'"
                    )
                )
        except RestorePointError:
            raise
        except Exception:
            raise RestorePointError("scratch_database_not_created") from None
        return str(row.owner)

    def _drop(self, admin_url: URL, name: str) -> None:
        try:
            with self._admin(admin_url) as connection:
                connection.execute(
                    text(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
                )
        except Exception:
            raise RestorePointError("scratch_database_not_dropped") from None

    def _restore_into(self, point: RestorePoint, url: URL, database: str) -> int:
        """Restore the dump into ``database`` and check it; its table count."""
        target = _sync_url(url, database)
        # The TOC listings sit beside the dump (the private directory).
        with tempfile.TemporaryDirectory(
            prefix=".toc-", dir=point.dump_path.parent
        ) as scratch:
            listing = Path(scratch) / "toc.list"
            self._run(
                [self._pg_restore, "--list", f"--file={listing}", str(point.dump_path)],
                {},
                "dump_unreadable",
            )
            selected, extensions = _read_toc(listing)
            use_list = []
            if self._template is not None:
                self._check_template_copy(target, extensions)
                chosen = Path(scratch) / "toc.selected"
                _write_private(chosen, "".join(line + "\n" for line in selected))
                use_list = [f"--use-list={chosen}"]
            elif extensions:
                self._check_extensions_creatable(target, extensions)
            self._run(
                [
                    self._pg_restore,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-password",
                    *use_list,
                    f"--dbname={database}",
                    str(point.dump_path),
                ],
                pg_environment(url, database=database),
                "restore_failed",
            )
        if not extensions <= self._extensions(target, "restored_database_unreadable"):
            raise RestorePointError("restored_extension_missing")
        if self._revision(target) != point.revision:
            raise RestorePointError("restored_revision_differs")
        engine = self._engine(target)
        try:
            with engine.connect() as connection:
                tables = connection.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN"
                        " ('pg_catalog', 'information_schema')"
                    )
                ).scalar_one()
        except Exception:
            raise RestorePointError("restored_database_unreadable") from None
        finally:
            engine.dispose()
        if tables < 1:
            raise RestorePointError("restored_database_empty")
        return int(tables)

    def _check_template_copy(self, url: URL, required: set[str]) -> None:
        """The fresh copy of the restore template: the dump's extensions, no
        other and no table (what the template holds would be in the restore)."""
        present = self._extensions(url, "restore_template_unreadable")
        if not required <= present:
            raise RestorePointError("restore_template_extension_missing")
        if present - required - _BUILT_IN_EXTENSIONS:
            raise RestorePointError("restore_template_unexpected_extension")
        if self._scalar(
            url,
            "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN"
            " ('pg_catalog', 'information_schema')",
            code="restore_template_unreadable",
        ):
            raise RestorePointError("restore_template_not_empty")

    def _check_extensions_creatable(self, url: URL, required: set[str]) -> None:
        """Without a restore template the dump creates its extensions: refused
        before ``pg_restore`` when the role may not create one of them (it is
        not a superuser and the extension is not trusted, or not installed)."""
        blocked = self._scalar(
            url,
            "SELECT count(*) FROM unnest(CAST(:names AS text[])) AS wanted(name)"
            " WHERE NOT (SELECT rolsuper FROM pg_roles"
            " WHERE rolname = current_user)"
            " AND NOT EXISTS (SELECT 1 FROM pg_available_extension_versions v"
            " JOIN pg_available_extensions a ON a.name = v.name"
            " AND a.default_version = v.version"
            " WHERE v.name = wanted.name AND (NOT v.superuser OR v.trusted))",
            code="restored_database_unreadable",
            names=sorted(required),
        )
        if blocked:
            raise RestorePointError("extension_needs_restore_template")


def _read_toc(listing: Path) -> tuple[list[str], set[str]]:
    """``pg_restore --list``'s output: its lines for a restore into a copy of
    the restore template (the extensions and their comments commented out) and
    the extensions the dump creates."""
    try:
        lines = listing.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise RestorePointError("dump_unreadable") from None
    entries = 0
    selected: list[str] = []
    extensions: set[str] = set()
    for line in lines:
        match = _TOC_ENTRY.fullmatch(line.rstrip())
        if match is None:
            if line.strip() and not line.lstrip().startswith(";"):
                raise RestorePointError("dump_unreadable")
            selected.append(line)
            continue
        entries += 1
        words = match.group(1).split()
        if words[:2] == ["EXTENSION", "-"] and len(words) >= 3:
            extensions.add(words[2])
            selected.append(";" + line)
        elif words[:3] == ["COMMENT", "-", "EXTENSION"]:
            selected.append(";" + line)
        else:
            selected.append(line)
    if entries == 0:
        raise RestorePointError("dump_unreadable")
    return selected, extensions


class _Connection:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __enter__(self):
        self._connection = self._engine.connect()
        return self._connection

    def __exit__(self, *exc_info) -> None:
        try:
            self._connection.close()
        finally:
            self._engine.dispose()
