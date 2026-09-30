"""Reading the records of a Recovery Repository back (format 1; Decision 0054 8, 9).

Every record is checked against the keys and types ``render.py`` writes: a
missing or unknown key, a wrong type or a file name that is not its record's id
is ``RecordError`` (a restore refuses the whole source). This is the reader of
format 1; a later format adds its own reader here and maps it to the current
schema (the "Migration Layer" of REQUIREMENTS.md "Versioning").
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from paw_backend.recovery.format import RecoveryFormatError, parse_utc


class RecordError(RecoveryFormatError):
    """A record is not the format's."""


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise RecordError("not an id")
    try:
        parsed = UUID(value)
    except ValueError:
        raise RecordError("not an id") from None
    if str(parsed) != value:
        raise RecordError("not an id")
    return parsed


def _time(value: object) -> datetime:
    try:
        return parse_utc(value)
    except RecoveryFormatError:
        raise RecordError("not a time") from None


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise RecordError("not a text")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RecordError("not an integer")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise RecordError("not a boolean")
    return value


def _seconds(value: object) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecordError("not a number of seconds")
    return timedelta(seconds=value)


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecordError("not a list of texts")
    return list(value)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecordError("not an object")
    return value


def _optional(parse: Callable[[object], Any]) -> Callable[[object], Any]:
    def parse_optional(value: object) -> Any:
        return None if value is None else parse(value)

    return parse_optional


Fields = Mapping[str, Callable[[object], Any]]


def parse_fields(
    value: object, fields: Fields, *, optional: Fields | None = None
) -> dict[str, Any]:
    """``value`` as a record with exactly ``fields`` (and maybe ``optional``)."""
    record = _object(value)
    optional = optional or {}
    keys = set(record)
    if not set(fields) <= keys or not keys <= set(fields) | set(optional):
        raise RecordError("the keys of a record are not the format's")
    result = {name: parse(record[name]) for name, parse in fields.items()}
    for name, parse in optional.items():
        if name in record:
            result[name] = parse(record[name])
    return result


def parse_list(value: object, fields: Fields, **options) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RecordError("not a list")
    return [parse_fields(item, fields, **options) for item in value]


QUOTA_FIELDS: Fields = {
    "kind": _str,
    "metric": _str,
    "period": _str,
    "limit_value": _optional(_int),
    "created_at": _time,
    "updated_at": _time,
}
USER_FIELDS: Fields = {
    "id": _uuid,
    "login_name": _str,
    "system_role": _str,
    "status": _str,
    "passkey_required": _bool,
    "created_at": _time,
    "updated_at": _time,
    "connection_quotas": lambda value: parse_list(value, QUOTA_FIELDS),
}
USER_OPTIONAL: Fields = {"login_name_redacted": _bool}
DELETION_FIELDS: Fields = {"id": _uuid, "status": _str}
MEMBER_FIELDS: Fields = {
    "user_id": _uuid,
    "role": _str,
    "status": _str,
    "invited_at": _time,
    "invite_expires_at": _optional(_time),
    "joined_at": _optional(_time),
}
PROJECT_FIELDS: Fields = {
    "id": _uuid,
    "name": _str,
    "description": _optional(_str),
    "status": _str,
    "created_by": _optional(_uuid),
    "created_at": _time,
    "updated_at": _time,
    "deletion_started_at": _optional(_time),
    "deletion_scheduled_at": _optional(_time),
    "deleted_at": _optional(_time),
    "members": lambda value: parse_list(value, MEMBER_FIELDS),
}
REMOTE_FIELDS: Fields = {"url": _str, "created_at": _time}
REPO_FIELDS: Fields = {
    "id": _uuid,
    "project_id": _uuid,
    "name": _str,
    "default_branch": _str,
    "source": _str,
    "acl_allowed": _optional(_strings),
    "created_by": _optional(_uuid),
    "created_at": _time,
    "updated_at": _time,
    "remotes": lambda value: parse_list(value, REMOTE_FIELDS),
}
SOURCE_FIELDS: Fields = {
    "id": _uuid,
    "source_type": _str,
    "source_ref": _optional(_str),
    "source_deleted_at": _optional(_time),
    "created_at": _time,
}
VERSION_FIELDS: Fields = {
    "id": _uuid,
    "version_number": _int,
    "scope": _str,
    "owner_user_id": _optional(_uuid),
    "project_id": _optional(_uuid),
    "project_group_id": _optional(_uuid),
    "repo_id": _optional(_uuid),
    "memory_type": _str,
    "title": _str,
    "content": _str,
    "importance": _int,
    "pinned": _bool,
    "status": _str,
    "confirmation_state": _str,
    "freshness_policy": _str,
    "verified_at": _optional(_time),
    "revalidate_after": _optional(_seconds),
    "revalidate_triggers": _strings,
    "on_stale": _str,
    "expires_at": _optional(_time),
    "commit_sha": _optional(_str),
    "branch": _optional(_str),
    "stale_since": _optional(_time),
    "attributes": _object,
    "actor_type": _str,
    "actor_user_id": _optional(_uuid),
    "change_reason": _optional(_str),
    "created_at": _time,
    "sources": lambda value: parse_list(value, SOURCE_FIELDS),
}
VERSION_OPTIONAL: Fields = {"redactions": _int, "truncated": _bool}
RELATION_FIELDS: Fields = {
    "id": _uuid,
    "from_version_id": _uuid,
    "to_version_id": _uuid,
    "relation_type": _str,
    "reason": _optional(_str),
    "created_at": _time,
}
MEMORY_FIELDS: Fields = {
    "id": _uuid,
    "created_at": _time,
    "versions": lambda value: parse_list(
        value, VERSION_FIELDS, optional=VERSION_OPTIONAL
    ),
    "relations": lambda value: parse_list(value, RELATION_FIELDS),
}
AUTH_POLICY_FIELDS: Fields = {
    "version": _int,
    "passkey_owner": _str,
    "passkey_admin": _str,
    "passkey_user": _str,
    "recommend_passkey_to_users": _bool,
    "stepup_window_minutes": _int,
    "updated_at": _time,
}
CONNECTION_FIELDS: Fields = {
    "kind": _str,
    "status": _str,
    "enabled": _bool,
    "created_at": _time,
    "updated_at": _time,
}
TASK_REPOSITORY_FIELDS: Fields = {
    "repository_id": _uuid,
    "branch": _optional(_str),
    "head_commit": _optional(_str),
    "review_status": _str,
    "evaluation_result": _str,
    "pr_number": _optional(_int),
    "pr_url": _optional(_str),
    "pr_state": _optional(_str),
}
TASK_FIELDS: Fields = {
    "id": _uuid,
    "project_id": _uuid,
    "created_by": _uuid,
    "title": _str,
    "state": _str,
    "wait_reason": _optional(_str),
    "attempt": _int,
    "retry_count": _int,
    "created_at": _time,
    "updated_at": _time,
    "repositories": lambda value: parse_list(value, TASK_REPOSITORY_FIELDS),
}


__all__ = [
    "AUTH_POLICY_FIELDS",
    "CONNECTION_FIELDS",
    "DELETION_FIELDS",
    "MEMORY_FIELDS",
    "PROJECT_FIELDS",
    "REPO_FIELDS",
    "TASK_FIELDS",
    "USER_FIELDS",
    "USER_OPTIONAL",
    "RecordError",
    "parse_fields",
    "parse_list",
]
