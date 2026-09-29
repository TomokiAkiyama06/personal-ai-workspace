"""Restore a workspace from its Recovery Repository (PAW-047, Decision 0054 6-12).

Recovery Import is for a **fresh** installation (REQUIREMENTS.md "Recovery
target": fresh Ubuntu, install, migrate, point at the Recovery Repository,
restore, re-register the credentials). The safe default:

* **Dry run first.** ``run(apply=False)`` (the command's default) checks
  everything and reports what would be restored; nothing is written but its
  audit row (``recovery.restore.planned``).
* **Nothing is overwritten or deleted.** The target must be empty: no user,
  project, repository, memory or quota (``target_not_empty`` otherwise). There is
  no merge and no partial restore (per user or project) in V1.
* **All or nothing.** ``apply=True`` inserts every row in one transaction, with
  the tables locked and the target re-checked inside it, and records
  ``recovery.restore.applied`` in the same transaction.
* **Only a verified source.** The checkout must be the recovery one (marker), its
  work tree clean and ``HEAD`` exactly the remote-tracking branch (the latest
  pushed state, whose deletion records are then applied: no personal data of a
  user in deletion is restored); the manifest's format must be one this code
  reads and every file must match ``recovery/checksums.sha256`` (no file missing,
  none unlisted); the target database must be at this release's head and the
  backup's schema one of this release's revisions.

Not restored (reported as manual steps): credentials of any kind (users come back
without a password or Passkey: the Owner uses ``owner-recover``, the others are
reset by the Owner / an Admin), the auth policy (a security setting: changed by
the Owner with a Step-up), the shared connections (re-registered), the
checkouts (cloned again), tasks (a summary only), conversation sources of
memories (conversations are not backed up), the audit trail.
"""

import functools
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.connections.models import ConnectionQuotaRow
from paw_backend.db import Database
from paw_backend.identity.models import UserRow
from paw_backend.memory.models import (
    Memory,
    MemoryRelation,
    MemorySource,
    MemoryVersion,
)
from paw_backend.memory.projection.runner import _in_thread
from paw_backend.memory.projection.writer import system_home_directories
from paw_backend.projects.models import ProjectMemberRow, ProjectRow
from paw_backend.recovery.audit import (
    RecoveryAction,
    insert_recovery_row,
    record_recovery_outcome,
)
from paw_backend.recovery.files import (
    RecoveryCheckout,
    RecoveryFilesError,
    check_directory_path,
    open_checkout,
)
from paw_backend.recovery.format import (
    AUTH_POLICY_PATH,
    CHECKSUMS_PATH,
    DELETIONS_DIRECTORY,
    MANIFEST_NAME,
    MARKER_NAME,
    MEMORY_DIRECTORY,
    MEMORY_RECORDS_DIRECTORY,
    PROJECTS_DIRECTORY,
    REPOS_DIRECTORY,
    SHARED_CONNECTIONS_PATH,
    SUPPORTED_FORMAT_VERSIONS,
    TASKS_DIRECTORY,
    USERS_DIRECTORY,
    Manifest,
    RecoveryFormatError,
    parse_checksums,
    parse_manifest,
    sha256_hex,
)
from paw_backend.recovery.git import (
    DEFAULT_TIMEOUT_SECONDS,
    RecoveryGit,
    RecoveryGitError,
)
from paw_backend.recovery.records import (
    AUTH_POLICY_FIELDS,
    CONNECTION_FIELDS,
    DELETION_FIELDS,
    MEMORY_FIELDS,
    PROJECT_FIELDS,
    REPO_FIELDS,
    TASK_FIELDS,
    USER_FIELDS,
    RecordError,
    parse_fields,
    parse_list,
)
from paw_backend.recovery.render import DELETION_STATUSES, is_record_name
from paw_backend.recovery.schema import (
    SchemaUnknownError,
    known_revisions,
    migration_head,
)
from paw_backend.repositories.models import RepositoryRemoteRow, RepositoryRow

Clock = Callable[[], datetime]

# The tables a target must have no row in (and that the restore writes).
TARGET_TABLES = (
    "users",
    "connection_quotas",
    "projects",
    "project_members",
    "repositories",
    "repository_remotes",
    "memories",
    "memory_versions",
    "memory_relations",
    "memory_sources",
)


class RestoreProblem(StrEnum):
    """Why a restore was refused (a closed vocabulary)."""

    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_INVALID = "manifest_invalid"
    FORMAT_UNSUPPORTED = "format_unsupported"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MISSING_FILE = "missing_file"
    UNLISTED_FILE = "unlisted_file"
    RECORD_INVALID = "record_invalid"
    DELETED_USER_DATA = "deleted_user_data"
    SOURCE_SCHEMA_UNKNOWN = "source_schema_unknown"
    TARGET_SCHEMA_MISMATCH = "target_schema_mismatch"
    SCHEMA_UNKNOWN = "schema_unknown"
    TARGET_NOT_EMPTY = "target_not_empty"


class RecoveryRestoreError(Exception):
    """A restore was refused (see ``problem``); nothing was written."""

    def __init__(self, problem: RestoreProblem) -> None:
        self.problem = problem
        super().__init__(f"recovery restore refused: {problem.value}")


@dataclass(slots=True)
class RestoreData:
    """The rows a restore inserts, and what it reports."""

    commit: str
    manifest: Manifest
    users: list[dict[str, Any]] = field(default_factory=list)
    quotas: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)
    repositories: list[dict[str, Any]] = field(default_factory=list)
    remotes: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    deletions: list[dict[str, Any]] = field(default_factory=list)
    skipped_conversation_sources: int = 0
    tasks: int = 0
    auth_policy: dict[str, Any] | None = None
    connections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "users": len(self.users),
            "connection_quotas": len(self.quotas),
            "projects": len(self.projects),
            "project_members": len(self.members),
            "repositories": len(self.repositories),
            "repository_remotes": len(self.remotes),
            "memories": len(self.memories),
            "memory_versions": len(self.versions),
            "memory_relations": len(self.relations),
            "memory_sources": len(self.sources),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What one restore did: ``applied`` or only planned, or why it was refused."""

    applied: bool = False
    data: RestoreData | None = None
    manual_steps: tuple[str, ...] = ()
    refused: str | None = None
    failed: str | None = None
    audited: bool = False

    @property
    def ok(self) -> bool:
        return self.refused is None and self.failed is None


def _refuse(problem: RestoreProblem) -> RecoveryRestoreError:
    return RecoveryRestoreError(problem)


def verify_files(files: Mapping[str, bytes]) -> Manifest:
    """The manifest of ``files`` once every checksum matched (or refuse)."""
    if MANIFEST_NAME not in files:
        raise _refuse(RestoreProblem.MANIFEST_MISSING)
    try:
        manifest = parse_manifest(files[MANIFEST_NAME])
    except RecoveryFormatError:
        raise _refuse(RestoreProblem.MANIFEST_INVALID) from None
    if manifest.recovery_format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise _refuse(RestoreProblem.FORMAT_UNSUPPORTED)
    checksums = files.get(CHECKSUMS_PATH)
    if checksums is None:
        raise _refuse(RestoreProblem.MISSING_FILE)
    if sha256_hex(checksums) != manifest.checksums_sha256:
        raise _refuse(RestoreProblem.CHECKSUM_MISMATCH)
    try:
        listed = parse_checksums(checksums)
    except RecoveryFormatError:
        raise _refuse(RestoreProblem.MANIFEST_INVALID) from None
    for path, digest in listed.items():
        if path not in files:
            raise _refuse(RestoreProblem.MISSING_FILE)
        if sha256_hex(files[path]) != digest:
            raise _refuse(RestoreProblem.CHECKSUM_MISMATCH)
    for path in files:
        if path not in listed and path not in (MANIFEST_NAME, CHECKSUMS_PATH):
            raise _refuse(RestoreProblem.UNLISTED_FILE)
    if manifest.files != len(listed) + 1:
        raise _refuse(RestoreProblem.MANIFEST_INVALID)
    return manifest


def _json(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RecordError("a record is not JSON") from None


def _records(
    files: Mapping[str, bytes], directory: str, fields
) -> list[dict[str, Any]]:
    records = []
    prefix = directory + "/"
    for path in sorted(files):
        if not path.startswith(prefix):
            continue
        name = path[len(prefix) :]
        if "/" in name or not is_record_name(name):
            raise RecordError("a record file name is not the format's")
        record = parse_fields(_json(files[path]), fields)
        if str(record["id"]) != name[: -len(".json")]:
            raise RecordError("a record file is not named by its id")
        records.append(record)
    return records


_FIXED_PATHS = frozenset(
    {
        MARKER_NAME,
        MANIFEST_NAME,
        CHECKSUMS_PATH,
        AUTH_POLICY_PATH,
        SHARED_CONNECTIONS_PATH,
    }
)
_RECORD_DIRECTORIES = (
    USERS_DIRECTORY,
    f"{DELETIONS_DIRECTORY}/{USERS_DIRECTORY}",
    PROJECTS_DIRECTORY,
    REPOS_DIRECTORY,
    MEMORY_RECORDS_DIRECTORY,
    TASKS_DIRECTORY,
)


def _known_path(path: str) -> bool:
    """A path format 1 writes (the memory copy is read by nobody but people)."""
    if path in _FIXED_PATHS or path.startswith(f"{MEMORY_DIRECTORY}/"):
        return True
    directory, _, name = path.rpartition("/")
    return directory in _RECORD_DIRECTORIES and is_record_name(name)


def parse_source(
    files: Mapping[str, bytes], commit: str, manifest: Manifest
) -> RestoreData:
    """The rows of a verified source (``RecordError`` / ``RecoveryRestoreError``)."""
    for path in files:
        if not _known_path(path):
            raise RecordError("a file the format does not have")
    users = _records(files, USERS_DIRECTORY, USER_FIELDS)
    deletions = _records(
        files, f"{DELETIONS_DIRECTORY}/{USERS_DIRECTORY}", DELETION_FIELDS
    )
    projects = _records(files, PROJECTS_DIRECTORY, PROJECT_FIELDS)
    repositories = _records(files, REPOS_DIRECTORY, REPO_FIELDS)
    memories = _records(files, MEMORY_RECORDS_DIRECTORY, MEMORY_FIELDS)
    tasks = _records(files, TASKS_DIRECTORY, TASK_FIELDS)
    policy_value = _json(files[AUTH_POLICY_PATH]) if AUTH_POLICY_PATH in files else None
    auth_policy = (
        None if policy_value is None else parse_fields(policy_value, AUTH_POLICY_FIELDS)
    )
    connections = (
        parse_list(_json(files[SHARED_CONNECTIONS_PATH]), CONNECTION_FIELDS)
        if SHARED_CONNECTIONS_PATH in files
        else []
    )

    deleted = {record["id"] for record in deletions}
    if any(record["status"] not in DELETION_STATUSES for record in deletions):
        raise RecordError("a deletion record without a deletion status")
    user_ids = {record["id"] for record in users}
    if user_ids & deleted or any(
        record["status"] in DELETION_STATUSES for record in users
    ):
        raise _refuse(RestoreProblem.DELETED_USER_DATA)
    for path in files:
        parts = path.split("/")
        if (
            parts[0] == MEMORY_DIRECTORY
            and len(parts) > 2
            and parts[1] == USERS_DIRECTORY
            and parts[2] in {str(user_id) for user_id in deleted}
        ):
            raise _refuse(RestoreProblem.DELETED_USER_DATA)

    data = RestoreData(
        commit=commit,
        manifest=manifest,
        deletions=deletions,
        tasks=len(tasks),
        auth_policy=auth_policy,
        connections=connections,
    )
    for record in users:
        quotas = record.pop("connection_quotas")
        data.users.append(record)
        for quota in quotas:
            data.quotas.append({"user_id": record["id"], **quota})
    for record in projects:
        members = record.pop("members")
        if record["created_by"] not in user_ids:
            record["created_by"] = None
        data.projects.append(record)
        for member in members:
            if member["user_id"] in deleted:
                raise _refuse(RestoreProblem.DELETED_USER_DATA)
            if member["user_id"] not in user_ids:
                raise RecordError("a member who is not a user")
            data.members.append({"project_id": record["id"], **member})
    for record in repositories:
        remotes = record.pop("remotes")
        if record["created_by"] not in user_ids:
            record["created_by"] = None
        data.repositories.append(record)
        for remote in remotes:
            data.remotes.append(
                {
                    "repository_id": record["id"],
                    "project_id": record["project_id"],
                    **remote,
                }
            )
    version_ids: set[UUID] = set()
    for record in memories:
        versions = record.pop("versions")
        relations = record.pop("relations")
        if not versions:
            raise RecordError("a memory without a version")
        data.memories.append(record)
        for version in versions:
            if version["scope"] == "user" and version["owner_user_id"] in deleted:
                raise _refuse(RestoreProblem.DELETED_USER_DATA)
            sources = version.pop("sources")
            version.pop("redactions", None)
            version.pop("truncated", None)
            data.versions.append({"memory_id": record["id"], **version})
            version_ids.add(version["id"])
            for source in sources:
                if source["source_type"] == "conversation":
                    # The conversation is not in the backup, and a new source of
                    # this type must name one (a database trigger).
                    data.skipped_conversation_sources += 1
                    continue
                data.sources.append({"memory_version_id": version["id"], **source})
        data.relations.extend(relations)
    for relation in data.relations:
        if (
            relation["from_version_id"] not in version_ids
            or relation["to_version_id"] not in version_ids
        ):
            raise RecordError("a relation to a version that is not restored")
    return data


def open_source(
    path: str | Path,
    protected: Collection[str],
    *,
    git_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[RecoveryCheckout, RestoreData]:
    """Lock, check and read the checkout at ``path`` (blocking).

    The checkout is returned **still locked**: the caller holds it until the
    restore is over, so no backup rewrites or pushes the repository between the
    check and the write (the caller closes it)."""
    text = check_directory_path(path, protected)
    git = RecoveryGit(text, timeout=git_timeout)
    git.check_top_level()
    checkout = open_checkout(text, protected, claim=False)
    try:
        commit = git.check_restorable()
        files = checkout.read_managed()
        manifest = verify_files(files)
        try:
            data = parse_source(files, commit, manifest)
        except RecordError:
            raise _refuse(RestoreProblem.RECORD_INVALID) from None
    except BaseException:
        checkout.close()
        raise
    return checkout, data


def load_source(
    path: str | Path,
    protected: Collection[str],
    *,
    git_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RestoreData:
    """``open_source`` without keeping the lock (for a check only)."""
    checkout, data = open_source(path, protected, git_timeout=git_timeout)
    checkout.close()
    return data


def _code(error: BaseException) -> str:
    if isinstance(error, RecoveryRestoreError | RecoveryFilesError | RecoveryGitError):
        return error.problem.value
    return type(error).__name__


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _target_counts(session: AsyncSession) -> dict[str, int]:
    counts = {}
    for table in TARGET_TABLES:
        # The names are this module's constants, never input.
        counts[table] = (
            await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        ).scalar_one()
    return counts


def manual_steps(
    data: RestoreData, current_policy: Mapping[str, Any] | None
) -> list[str]:
    """What the operator does after an applied restore (printed by the command)."""
    steps = [
        "Credentials are not restored: the Owner runs `sudo python -m "
        "paw_backend.cli owner-recover --confirm-owner-recovery` and registers a "
        "Passkey; the Owner (or an Admin) resets every other account "
        "(Decision 0032).",
    ]
    if data.repositories:
        steps.append(
            f"Clone the {len(data.repositories)} repositories again (checkouts "
            "are not restored); each user logs in to GitHub again (gh auth login)."
        )
    if data.connections:
        kinds = ", ".join(sorted(str(row["kind"]) for row in data.connections))
        steps.append(f"Register the shared connections again ({kinds}).")
    if data.auth_policy is not None and current_policy is not None:
        keys = [key for key in data.auth_policy if key not in ("version", "updated_at")]
        if any(data.auth_policy[key] != current_policy.get(key) for key in keys):
            steps.append(
                "The backed-up auth policy differs from this installation's: the "
                "Owner sets it again in the settings (Step-up)."
            )
    if data.skipped_conversation_sources:
        steps.append(
            f"{data.skipped_conversation_sources} conversation source(s) of "
            "memories were not restored (conversations are not backed up)."
        )
    # Always: a deletion that began after the last push is in no record here.
    steps.append(
        f"{len(data.deletions)} user(s) in deletion in this backup were not "
        "restored. A deletion that began after the last push is not in it: check "
        "the deletion records kept outside this backup, and apply them, before "
        "resuming operation (Decision 0054, 11)."
    )
    steps.append("Run memory-projection-run to regenerate the Markdown projection.")
    return steps


class RecoveryRestorer:
    """``run(apply=...)``: one restore (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        repository_dir: str | Path,
        *,
        protected_homes: Collection[str] | None = None,
        clock: Clock = _utc_now,
        git_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        target_head: Callable[[], str] = migration_head,
        revisions: Callable[[], frozenset[str]] = known_revisions,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._repository_dir = str(repository_dir)
        self._protected = (
            system_home_directories()
            if protected_homes is None
            else tuple(protected_homes)
        )
        self._clock = clock
        self._git_timeout = git_timeout
        self._target_head = target_head
        self._revisions = revisions

    async def _check_target(self, session: AsyncSession, data: RestoreData) -> None:
        try:
            revisions, head = self._revisions(), self._target_head()
        except SchemaUnknownError:
            raise _refuse(RestoreProblem.SCHEMA_UNKNOWN) from None
        if data.manifest.workspace_schema_version not in revisions:
            raise _refuse(RestoreProblem.SOURCE_SCHEMA_UNKNOWN)
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
        if revision != head:
            raise _refuse(RestoreProblem.TARGET_SCHEMA_MISMATCH)
        if any((await _target_counts(session)).values()):
            raise _refuse(RestoreProblem.TARGET_NOT_EMPTY)

    async def _current_policy(self, session: AsyncSession) -> dict[str, Any] | None:
        row = (
            await session.execute(
                text(
                    "SELECT passkey_owner, passkey_admin, passkey_user,"
                    " recommend_passkey_to_users, stepup_window_minutes"
                    " FROM auth_policy ORDER BY id LIMIT 1"
                )
            )
        ).first()
        return None if row is None else dict(row._mapping)

    async def _record(self, action: RecoveryAction, reason: str) -> bool:
        try:
            await record_recovery_outcome(
                self._database, action, reason, occurred_at=self._clock()
            )
        except Exception:
            return False
        return True

    async def run(self, *, apply: bool = False) -> RestoreResult:
        try:
            checkout, data = await _in_thread(
                functools.partial(
                    open_source,
                    self._repository_dir,
                    self._protected,
                    git_timeout=self._git_timeout,
                ),
                discard=lambda value: value[0].close(),
            )
        except (RecoveryRestoreError, RecoveryFilesError, RecoveryGitError) as refusal:
            code = _code(refusal)
            audited = await self._record(RecoveryAction.RESTORE_REFUSED, code)
            return RestoreResult(refused=code, audited=audited)
        try:
            return await self._restore(data, apply=apply)
        finally:
            # Held through the checks, the write and its audit row.
            await _in_thread(checkout.close)

    async def _restore(self, data: RestoreData, *, apply: bool) -> RestoreResult:
        try:
            async with self._database.session() as session, session.begin():
                await self._check_target(session, data)
                policy = await self._current_policy(session)
        except RecoveryRestoreError as refusal:
            code = _code(refusal)
            audited = await self._record(RecoveryAction.RESTORE_REFUSED, code)
            return RestoreResult(data=data, refused=code, audited=audited)
        steps = tuple(manual_steps(data, policy))
        counts = data.counts
        reason = (
            f"users={counts['users']} projects={counts['projects']} "
            f"repos={counts['repositories']} memories={counts['memories']} "
            f"versions={counts['memory_versions']}"
        )
        if not apply:
            audited = await self._record(RecoveryAction.RESTORE_PLANNED, reason)
            if not audited:
                # Every run leaves its audit row; a dry run without one failed.
                return RestoreResult(data=data, failed="audit_unrecorded")
            return RestoreResult(data=data, manual_steps=steps, audited=True)
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    text(
                        "LOCK TABLE "
                        + ", ".join(TARGET_TABLES)
                        + " IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                await self._check_target(session, data)
                await _insert_all(session, data)
                await insert_recovery_row(
                    session,
                    RecoveryAction.RESTORE_APPLIED,
                    reason,
                    occurred_at=self._clock(),
                )
        except RecoveryRestoreError as refusal:
            code = _code(refusal)
            audited = await self._record(RecoveryAction.RESTORE_REFUSED, code)
            return RestoreResult(data=data, refused=code, audited=audited)
        except Exception as failure:
            code = _code(failure)
            audited = await self._record(RecoveryAction.RESTORE_FAILED, code)
            return RestoreResult(data=data, failed=code, audited=audited)
        return RestoreResult(applied=True, data=data, manual_steps=steps, audited=True)


async def _insert(session: AsyncSession, table, rows: list[dict[str, Any]]) -> None:
    if rows:
        await session.execute(insert(table), rows)


async def _insert_all(session: AsyncSession, data: RestoreData) -> None:
    await _insert(session, UserRow.__table__, data.users)
    await _insert(session, ConnectionQuotaRow.__table__, data.quotas)
    await _insert(session, ProjectRow.__table__, data.projects)
    await _insert(session, ProjectMemberRow.__table__, data.members)
    await _insert(session, RepositoryRow.__table__, data.repositories)
    await _insert(session, RepositoryRemoteRow.__table__, data.remotes)
    await _insert(session, Memory.__table__, data.memories)
    await _insert(session, MemoryVersion.__table__, data.versions)
    await _insert(session, MemoryRelation.__table__, data.relations)
    await _insert(
        session,
        MemorySource.__table__,
        [
            {**source, "conversation_id": None, "message_id": None}
            for source in data.sources
        ],
    )


__all__ = [
    "TARGET_TABLES",
    "RecoveryRestoreError",
    "RecoveryRestorer",
    "RestoreData",
    "RestoreProblem",
    "RestoreResult",
    "load_source",
    "open_source",
    "manual_steps",
    "parse_source",
    "verify_files",
]
