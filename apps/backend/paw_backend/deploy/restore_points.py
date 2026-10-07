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
* **restore**: only a verified point whose dump still has its checksum. It is
  restored into a new database (``<db>_restore_<stamp>``) and checked as above;
  then the workspace database is renamed to ``<db>_replaced_<stamp>`` (kept for
  the operator to inspect and drop) and the new one takes its name. Restoring
  into a new database also removes what a failed migration created, which
  ``pg_restore --clean`` would leave behind.

Credentials go to ``pg_dump`` / ``pg_restore`` in their environment (``PGHOST``,
``PGUSER``, ``PGPASSWORD``, ...), never on the command line, and nothing printed
contains them. ``pg_dump`` / ``pg_restore`` must be of the server's major version
or newer.
"""

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

FORMAT_VERSION = 1
LABEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
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
    ) -> None:
        directory = Path(directory)
        if not directory.is_absolute():
            raise RestorePointError("directory_not_absolute")
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
        partial = directory / f".{label}.dump.partial"
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
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            size=dump.stat().st_size,
            sha256=_sha256(dump),
            verified=None,
            metadata_path=metadata,
        )
        _write_private(metadata, json.dumps(point.as_json(), indent=2) + "\n")
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
            connection.execute(
                text(
                    f"ALTER DATABASE {_quote(incoming)} "
                    f"RENAME TO {_quote(point.database)}"
                )
            )
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

    def _admin(self, admin_url: URL):
        engine = self._engine(_sync_url(admin_url), isolation_level="AUTOCOMMIT")
        return _Connection(engine)

    def _create_like(self, admin_url: URL, source: str, name: str) -> str:
        """Create ``name`` with the owner and encoding of ``source``; its owner."""
        try:
            with self._admin(admin_url) as connection:
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
                        f"CREATE DATABASE {_quote(name)} TEMPLATE template0"
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
        self._run(
            [
                self._pg_restore,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-password",
                f"--dbname={database}",
                str(point.dump_path),
            ],
            pg_environment(url, database=database),
            "restore_failed",
        )
        if self._revision(_sync_url(url, database)) != point.revision:
            raise RestorePointError("restored_revision_differs")
        engine = self._engine(_sync_url(url, database))
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
