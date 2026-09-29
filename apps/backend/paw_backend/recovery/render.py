"""The pure renderer of the Recovery Repository (PAW-047, Decision 0054).

``render_recovery`` turns one database snapshot (``source.RecoverySnapshot``) and
one copy of the Memory Markdown Projection into every file of the checkout
(``format``): the same input gives the same bytes, whatever order it comes in.

What is **left out** (Decision 0054 3, 4):

* A user in deletion (``pending_deletion`` / ``deleted``) keeps only a deletion
  record (``deletions/users/<id>.json``: id and status). Their user record, quotas,
  memberships, private (``user``-scope) memory versions and projection directory
  (``memory/users/<id>/``) are not in the current backup, so a restore never
  brings their personal data back. Older commits keep what they held (erasing
  Git history is the user-erasure flow's, with a human approval).
* ``session_only`` memory versions (not Long-term Memory).
* Relations and sources of versions that are left out.

Recognisable credentials in every free-text value (titles, texts, names, branch
names, references, reasons, ``attributes`` values) are replaced by
``[REDACTED]`` (``tools.credentials.redact_text`` / ``redact_value``), as the
Memory Projection does (Decision 0038 5); a text too long to scan is cut. A
version says so with ``redactions`` / ``truncated``. PostgreSQL keeps the text.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from paw_backend.memory.projection.render import is_uuid_name
from paw_backend.recovery.format import (
    AUTH_POLICY_PATH,
    CHECKSUMS_PATH,
    DELETIONS_DIRECTORY,
    MANIFEST_NAME,
    MARKER_CONTENT,
    MARKER_NAME,
    MEMORY_DIRECTORY,
    MEMORY_RECORDS_DIRECTORY,
    PROJECTS_DIRECTORY,
    RECOVERY_FORMAT_VERSION,
    REPOS_DIRECTORY,
    SHARED_CONNECTIONS_PATH,
    TASKS_DIRECTORY,
    USERS_DIRECTORY,
    Manifest,
    RecoveryFormatError,
    encode_json,
    is_valid_path,
    parse_manifest,
    render_checksums,
    sha256_hex,
    utc_text,
)
from paw_backend.recovery.source import RecoverySnapshot, Row
from paw_backend.tools.credentials import MAX_TEXT_CHARS, redact_text, redact_value

DELETION_STATUSES = frozenset({"pending_deletion", "deleted"})
SESSION_ONLY = "session_only"


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """Every file of the checkout (path -> bytes) and what went into it."""

    files: Mapping[str, bytes]
    counts: Mapping[str, int]
    redactions: int
    truncations: int


class _Redactor:
    def __init__(self) -> None:
        self.redactions = 0
        self.truncations = 0

    def text(self, value: str | None) -> tuple[str | None, int, bool]:
        if value is None:
            return None, 0, False
        truncated = len(value) > MAX_TEXT_CHARS
        result, count = redact_text(value)
        count -= truncated
        self.redactions += count
        self.truncations += truncated
        return result, count, truncated

    def plain(self, value: str | None) -> str | None:
        return self.text(value)[0]

    def value(self, value: object) -> tuple[object, int]:
        result, count = redact_value(value)
        self.redactions += count
        return result, count


def plain(value: object) -> object:
    """A column value as JSON: ids and times as text, an interval in seconds."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
        return int(seconds) if seconds == int(seconds) else seconds
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    raise RecoveryFormatError("a column value the format cannot hold")


def _fields(row: Row, names: Iterable[str]) -> dict[str, object]:
    return {name: plain(row[name]) for name in names}


LOGIN_PLACEHOLDER_PREFIX = "redacted-"


def login_placeholder(user_id: UUID) -> str:
    """The login name a user with a credential-shaped one gets in the backup."""
    return LOGIN_PLACEHOLDER_PREFIX + user_id.hex[:12]


def _user_file(user: Row, quotas: list[Row], redactor: _Redactor) -> bytes:
    record = _fields(
        user,
        (
            "id",
            "system_role",
            "status",
            "passkey_required",
            "created_at",
            "updated_at",
        ),
    )
    # A valid login name can match the credential detector: ``[REDACTED]`` is
    # not a valid login name, so the user is written under a placeholder made
    # from the id, and a restore asks the Owner to rename them (Decision 0054 3).
    _, count = redact_text(user["login_name"])
    if count:
        redactor.redactions += count
        record["login_name"] = login_placeholder(user["id"])
        record["login_name_redacted"] = True
    else:
        record["login_name"] = user["login_name"]
    record["connection_quotas"] = [
        _fields(
            quota,
            ("kind", "metric", "period", "limit_value", "created_at", "updated_at"),
        )
        for quota in quotas
    ]
    return encode_json(record)


def _project_file(project: Row, members: list[Row], redactor: _Redactor) -> bytes:
    record = _fields(
        project,
        (
            "id",
            "status",
            "created_by",
            "created_at",
            "updated_at",
            "deletion_started_at",
            "deletion_scheduled_at",
            "deleted_at",
        ),
    )
    record["name"] = redactor.plain(project["name"])
    record["description"] = redactor.plain(project["description"])
    record["members"] = [
        _fields(
            member,
            (
                "user_id",
                "role",
                "status",
                "invited_at",
                "invite_expires_at",
                "joined_at",
            ),
        )
        for member in members
    ]
    return encode_json(record)


def _repo_file(repository: Row, remotes: list[Row], redactor: _Redactor) -> bytes:
    record = _fields(
        repository,
        (
            "id",
            "project_id",
            "source",
            "acl_allowed",
            "created_by",
            "created_at",
            "updated_at",
        ),
    )
    # A name, a branch or a URL path can hold a token-shaped string (the
    # database only checks the characters): redacted like every free text. A
    # restore leaves such a repository or remote out (it has no valid name).
    record["name"] = redactor.plain(repository["name"])
    record["default_branch"] = redactor.plain(repository["default_branch"])
    record["remotes"] = []
    for remote in remotes:
        entry = _fields(remote, ("created_at",))
        entry["url"] = redactor.plain(remote["url"])
        record["remotes"].append(entry)
    return encode_json(record)


_VERSION_COLUMNS = (
    "id",
    "version_number",
    "scope",
    "owner_user_id",
    "project_id",
    "project_group_id",
    "repo_id",
    "importance",
    "pinned",
    "status",
    "confirmation_state",
    "freshness_policy",
    "verified_at",
    "revalidate_after",
    "revalidate_triggers",
    "on_stale",
    "expires_at",
    "commit_sha",
    "stale_since",
    "actor_type",
    "actor_user_id",
    "created_at",
)


def _version_record(
    version: Row, sources: list[Row], redactor: _Redactor
) -> dict[str, object]:
    record = _fields(version, _VERSION_COLUMNS)
    redactions = 0
    truncated = False
    for name in ("title", "content", "branch", "memory_type", "change_reason"):
        value, count, cut = redactor.text(version[name])
        record[name] = value
        redactions += count
        truncated = truncated or cut
    attributes, count = redactor.value(plain(version["attributes"] or {}))
    record["attributes"] = attributes
    redactions += count
    record["sources"] = []
    for source in sources:
        entry = _fields(
            source, ("id", "source_type", "source_deleted_at", "created_at")
        )
        value, count, _ = redactor.text(source["source_ref"])
        entry["source_ref"] = value
        redactions += count
        record["sources"].append(entry)
    if redactions:
        record["redactions"] = redactions
    if truncated:
        record["truncated"] = True
    return record


def _task_file(task: Row, repositories: list[Row], redactor: _Redactor) -> bytes:
    record = _fields(
        task,
        (
            "id",
            "project_id",
            "created_by",
            "state",
            "wait_reason",
            "attempt",
            "retry_count",
            "created_at",
            "updated_at",
        ),
    )
    record["title"] = redactor.plain(task["title"])
    record["repositories"] = []
    for repository in repositories:
        entry = _fields(
            repository,
            (
                "repository_id",
                "head_commit",
                "review_status",
                "evaluation_result",
                "pr_number",
                "pr_state",
            ),
        )
        entry["branch"] = redactor.plain(repository["branch"])
        entry["pr_url"] = redactor.plain(repository["pr_url"])
        record["repositories"].append(entry)
    return encode_json(record)


def _group(rows: Iterable[Row], key: str) -> dict[object, list[Row]]:
    grouped: dict[object, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def _memory_path(relative: str, excluded_users: set[str]) -> str | None:
    """``memory/<relative>``, or ``None`` for a user in deletion's directory."""
    parts = relative.split("/")
    if parts[0] == USERS_DIRECTORY and len(parts) > 1 and parts[1] in excluded_users:
        return None
    path = f"{MEMORY_DIRECTORY}/{relative}"
    if not is_valid_path(path):
        raise RecoveryFormatError("a projection file name is not the format's")
    return path


def render_recovery(
    snapshot: RecoverySnapshot,
    memory_files: Mapping[str, bytes],
    *,
    schema_version: str,
    workspace_version: str,
    now: datetime,
    previous_manifest: bytes | None = None,
) -> RecoveryPlan:
    """Every file of the checkout for ``snapshot`` and ``memory_files``.

    ``memory_files`` are the projection's files by their path below its root
    (``users/<id>/<memory id>.md``). ``previous_manifest`` is the manifest in the
    checkout: kept byte for byte when nothing else changed.
    """
    redactor = _Redactor()
    files: dict[str, bytes] = {MARKER_NAME: MARKER_CONTENT}
    excluded = {
        user["id"] for user in snapshot.users if user["status"] in DELETION_STATUSES
    }
    excluded_text = {str(user_id) for user_id in excluded}
    counts = {
        "deletions": 0,
        "memories": 0,
        "memory_files": 0,
        "memory_versions": 0,
        "projects": 0,
        "repos": 0,
        "tasks": 0,
        "users": 0,
    }

    quotas = _group(snapshot.quotas, "user_id")
    for user in snapshot.users:
        if user["id"] in excluded:
            record = {"id": str(user["id"]), "status": user["status"]}
            path = f"{DELETIONS_DIRECTORY}/{USERS_DIRECTORY}/{user['id']}.json"
            files[path] = encode_json(record)
            counts["deletions"] += 1
            continue
        files[f"{USERS_DIRECTORY}/{user['id']}.json"] = _user_file(
            user, quotas.get(user["id"], []), redactor
        )
        counts["users"] += 1

    members = _group(
        (row for row in snapshot.members if row["user_id"] not in excluded),
        "project_id",
    )
    for project in snapshot.projects:
        files[f"{PROJECTS_DIRECTORY}/{project['id']}.json"] = _project_file(
            project, members.get(project["id"], []), redactor
        )
        counts["projects"] += 1

    remotes = _group(snapshot.remotes, "repository_id")
    for repository in snapshot.repositories:
        files[f"{REPOS_DIRECTORY}/{repository['id']}.json"] = _repo_file(
            repository, remotes.get(repository["id"], []), redactor
        )
        counts["repos"] += 1

    kept_versions = [
        version
        for version in snapshot.versions
        if version["freshness_policy"] != SESSION_ONLY
        and not (version["scope"] == "user" and version["owner_user_id"] in excluded)
    ]
    kept_ids = {version["id"]: version["memory_id"] for version in kept_versions}
    sources = _group(
        (row for row in snapshot.sources if row["memory_version_id"] in kept_ids),
        "memory_version_id",
    )
    relations = _group(
        (
            row
            for row in snapshot.relations
            if row["from_version_id"] in kept_ids and row["to_version_id"] in kept_ids
        ),
        "from_version_id",
    )
    versions_by_memory = _group(kept_versions, "memory_id")
    for memory in snapshot.memories:
        versions = versions_by_memory.get(memory["id"])
        if not versions:
            continue
        record: dict[str, Any] = _fields(memory, ("id", "created_at"))
        record["versions"] = [
            _version_record(version, sources.get(version["id"], []), redactor)
            for version in sorted(versions, key=lambda row: row["version_number"])
        ]
        record["relations"] = []
        for version in versions:
            for relation in relations.get(version["id"], []):
                entry = _fields(
                    relation,
                    (
                        "id",
                        "from_version_id",
                        "to_version_id",
                        "relation_type",
                        "created_at",
                    ),
                )
                entry["reason"] = redactor.plain(relation["reason"])
                record["relations"].append(entry)
        record["relations"].sort(key=lambda entry: entry["id"])
        files[f"{MEMORY_RECORDS_DIRECTORY}/{memory['id']}.json"] = encode_json(record)
        counts["memories"] += 1
        counts["memory_versions"] += len(versions)

    policy = snapshot.auth_policy[0] if snapshot.auth_policy else None
    files[AUTH_POLICY_PATH] = encode_json(
        None
        if policy is None
        else _fields(
            policy,
            (
                "version",
                "passkey_owner",
                "passkey_admin",
                "passkey_user",
                "recommend_passkey_to_users",
                "stepup_window_minutes",
                "updated_at",
            ),
        )
    )
    files[SHARED_CONNECTIONS_PATH] = encode_json(
        [
            _fields(row, ("kind", "status", "enabled", "created_at", "updated_at"))
            for row in snapshot.connections
        ]
    )

    task_repositories = _group(snapshot.task_repositories, "task_id")
    for task in snapshot.tasks:
        files[f"{TASKS_DIRECTORY}/{task['id']}.json"] = _task_file(
            task, task_repositories.get(task["id"], []), redactor
        )
        counts["tasks"] += 1

    for relative in sorted(memory_files):
        path = _memory_path(relative, excluded_text)
        if path is None:
            continue
        files[path] = memory_files[relative]
        counts["memory_files"] += 1

    for path in files:
        if not is_valid_path(path):
            raise RecoveryFormatError("a path is not the format's")
    checksums = render_checksums(files)
    files[CHECKSUMS_PATH] = checksums
    files[MANIFEST_NAME] = _manifest(
        checksums,
        len(files),
        counts,
        schema_version=schema_version,
        workspace_version=workspace_version,
        now=now,
        previous=previous_manifest,
    )
    return RecoveryPlan(
        files=files,
        counts=counts,
        redactions=redactor.redactions,
        truncations=redactor.truncations,
    )


def _manifest(
    checksums: bytes,
    file_count: int,
    counts: Mapping[str, int],
    *,
    schema_version: str,
    workspace_version: str,
    now: datetime,
    previous: bytes | None,
) -> bytes:
    manifest = Manifest(
        recovery_format_version=RECOVERY_FORMAT_VERSION,
        workspace_schema_version=schema_version,
        workspace_version=workspace_version,
        generated_at=now,
        checksums_sha256=sha256_hex(checksums),
        files=file_count,
        counts=counts,
    )
    if previous is not None:
        try:
            old = parse_manifest(previous)
        except RecoveryFormatError:
            old = None
        if (
            old is not None
            and old.recovery_format_version == manifest.recovery_format_version
            and old.checksums_sha256 == manifest.checksums_sha256
            and old.workspace_schema_version == schema_version
            and old.workspace_version == workspace_version
            and old.to_json() == previous
        ):
            return previous
    return manifest.to_json()


def is_record_name(name: str) -> bool:
    """``<canonical uuid>.json``: a record file name."""
    return name.endswith(".json") and is_uuid_name(name[: -len(".json")])


__all__ = [
    "DELETION_STATUSES",
    "SESSION_ONLY",
    "RecoveryPlan",
    "is_record_name",
    "login_placeholder",
    "plain",
    "render_recovery",
]
