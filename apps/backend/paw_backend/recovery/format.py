"""The on-disk format of the Recovery Repository (PAW-047, Decision 0054 Proposed).

```text
<PAW_RECOVERY_REPOSITORY_DIR>/          # a git checkout of the private repository
├── .paw-recovery-repository            # marker: this checkout is the recovery one
├── manifest.json                       # format / schema version, counts, checksum
├── recovery/checksums.sha256           # sha256 of every other file (sha256sum -c)
├── memory/                             # copy of the Memory Markdown Projection
├── users/<user id>.json                # a user (no credential) and its quotas
├── deletions/users/<user id>.json      # a user in deletion: id and status only
├── projects/<project id>.json          # a project and its members (ACL)
├── repos/<repo id>.json                # a repository, its remotes and ACL
├── memory-records/<memory id>.json     # every version, relation and source
├── policies/*.json                     # the auth policy, the shared connections
└── tasks/<task id>.json                # a task's recovery summary
```

Everything here is the **machine-readable** half: deterministic JSON (keys sorted,
two-space indent, UTF-8, LF, one newline at the end; JSON is valid YAML), one file
per entity so a Git diff shows what changed. Only names this module lists are the
backup's (``MANAGED_ROOT_NAMES``); anything else in the checkout (a ``README.md``,
``.gitignore``) is never touched and never committed by the job.

``manifest.json`` changes only when a file changed (its ``generated_at`` is the
time of the first backup of that content), so a run without changes commits
nothing. ``recovery_format_version`` is what a restore checks first: a version
this code cannot read is refused; a later format gets a reader of the older one
here (the migration layer of REQUIREMENTS.md "Versioning").
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

RECOVERY_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = frozenset({RECOVERY_FORMAT_VERSION})

MARKER_NAME = ".paw-recovery-repository"
MARKER_CONTENT = (
    f"Personal AI Workspace recovery repository, format {RECOVERY_FORMAT_VERSION}.\n"
    "Written by recovery-backup-run; do not edit. See Decision 0054.\n"
).encode()
MANIFEST_NAME = "manifest.json"
CHECKSUMS_PATH = "recovery/checksums.sha256"

MEMORY_DIRECTORY = "memory"
USERS_DIRECTORY = "users"
DELETIONS_DIRECTORY = "deletions"
PROJECTS_DIRECTORY = "projects"
REPOS_DIRECTORY = "repos"
MEMORY_RECORDS_DIRECTORY = "memory-records"
POLICIES_DIRECTORY = "policies"
TASKS_DIRECTORY = "tasks"
RECOVERY_DIRECTORY = "recovery"
AUTH_POLICY_PATH = "policies/auth-policy.json"
SHARED_CONNECTIONS_PATH = "policies/shared-connections.json"

# The names at the root of the checkout the backup writes, replaces and removes.
# The marker is written once (claiming the checkout) and never replaced.
MANAGED_DIRECTORIES = (
    DELETIONS_DIRECTORY,
    MEMORY_DIRECTORY,
    MEMORY_RECORDS_DIRECTORY,
    POLICIES_DIRECTORY,
    PROJECTS_DIRECTORY,
    RECOVERY_DIRECTORY,
    REPOS_DIRECTORY,
    TASKS_DIRECTORY,
    USERS_DIRECTORY,
)
MANAGED_ROOT_NAMES = (MARKER_NAME, MANIFEST_NAME, *MANAGED_DIRECTORIES)

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RecoveryFormatError(ValueError):
    """A path, a manifest or a record is not the format's. A closed text."""


def is_valid_path(path: str) -> bool:
    """A relative path the backup can write: plain components, no ``.git``."""
    if path == MARKER_NAME:
        return True
    parts = path.split("/")
    if parts[0] not in MANAGED_ROOT_NAMES:
        return False
    return all(_COMPONENT.fullmatch(part) is not None for part in parts)


def encode_json(value: object) -> bytes:
    """``value`` as the format's deterministic JSON (see the module docstring)."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def utc_text(value: datetime) -> str:
    """A time as UTC ISO 8601 with ``Z`` (microseconds kept when present)."""
    if value.tzinfo is None:
        raise RecoveryFormatError("a time without a time zone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(text: object) -> datetime:
    if not isinstance(text, str) or not text.endswith("Z"):
        raise RecoveryFormatError("not a UTC time")
    try:
        value = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise RecoveryFormatError("not a UTC time") from None
    return value


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_checksums(files: Mapping[str, bytes]) -> bytes:
    """``<sha256>  <path>`` per file, sorted by path (``sha256sum -c`` reads it)."""
    lines = [f"{sha256_hex(files[path])}  {path}\n" for path in sorted(files)]
    return "".join(lines).encode("utf-8")


def parse_checksums(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RecoveryFormatError("checksums are not UTF-8") from None
    result: dict[str, str] = {}
    for line in text.splitlines():
        digest, separator, path = line.partition("  ")
        if (
            not separator
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not is_valid_path(path)
            or path in result
            or path in (MANIFEST_NAME, CHECKSUMS_PATH)
        ):
            raise RecoveryFormatError("a checksum line is not valid")
        result[path] = digest
    if not text.endswith("\n") and text:
        raise RecoveryFormatError("the checksums do not end with a newline")
    return result


@dataclass(frozen=True, slots=True)
class Manifest:
    recovery_format_version: int
    workspace_schema_version: str
    workspace_version: str
    generated_at: datetime
    checksums_sha256: str
    # Every file but the manifest: the listed ones and the checksums file.
    files: int
    counts: Mapping[str, int]

    def to_json(self) -> bytes:
        return encode_json(
            {
                "checksums_sha256": self.checksums_sha256,
                "counts": dict(sorted(self.counts.items())),
                "files": self.files,
                "generated_at": utc_text(self.generated_at),
                "recovery_format_version": self.recovery_format_version,
                "workspace_schema_version": self.workspace_schema_version,
                "workspace_version": self.workspace_version,
            }
        )


_MANIFEST_KEYS = frozenset(
    {
        "checksums_sha256",
        "counts",
        "files",
        "generated_at",
        "recovery_format_version",
        "workspace_schema_version",
        "workspace_version",
    }
)


def parse_manifest(data: bytes) -> Manifest:
    """The manifest, or ``RecoveryFormatError``. The format version is not judged."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RecoveryFormatError("the manifest is not JSON") from None
    if not isinstance(value, dict) or "recovery_format_version" not in value:
        raise RecoveryFormatError("the manifest has no format version")
    version = value["recovery_format_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise RecoveryFormatError("the manifest has no format version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        # Only the version is read from a format this code does not know.
        return Manifest(version, "", "", datetime.min.replace(tzinfo=UTC), "", 0, {})
    if set(value) != _MANIFEST_KEYS:
        raise RecoveryFormatError("the manifest keys are not the format's")
    counts = value["counts"]
    if not isinstance(counts, dict) or not all(
        isinstance(key, str)
        and isinstance(number, int)
        and not isinstance(number, bool)
        for key, number in counts.items()
    ):
        raise RecoveryFormatError("the manifest counts are not valid")
    for key in ("workspace_schema_version", "workspace_version", "checksums_sha256"):
        if not isinstance(value[key], str):
            raise RecoveryFormatError("a manifest value is not valid")
    if re.fullmatch(r"[0-9a-f]{64}", value["checksums_sha256"]) is None:
        raise RecoveryFormatError("a manifest value is not valid")
    files = value["files"]
    if not isinstance(files, int) or isinstance(files, bool) or files < 0:
        raise RecoveryFormatError("a manifest value is not valid")
    return Manifest(
        recovery_format_version=version,
        workspace_schema_version=value["workspace_schema_version"],
        workspace_version=value["workspace_version"],
        generated_at=parse_utc(value["generated_at"]),
        checksums_sha256=value["checksums_sha256"],
        files=files,
        counts=counts,
    )


__all__ = [
    "AUTH_POLICY_PATH",
    "CHECKSUMS_PATH",
    "DELETIONS_DIRECTORY",
    "MANAGED_DIRECTORIES",
    "MANAGED_ROOT_NAMES",
    "MANIFEST_NAME",
    "MARKER_CONTENT",
    "MARKER_NAME",
    "MEMORY_DIRECTORY",
    "MEMORY_RECORDS_DIRECTORY",
    "POLICIES_DIRECTORY",
    "PROJECTS_DIRECTORY",
    "RECOVERY_DIRECTORY",
    "RECOVERY_FORMAT_VERSION",
    "REPOS_DIRECTORY",
    "SHARED_CONNECTIONS_PATH",
    "SUPPORTED_FORMAT_VERSIONS",
    "TASKS_DIRECTORY",
    "USERS_DIRECTORY",
    "Manifest",
    "RecoveryFormatError",
    "encode_json",
    "is_valid_path",
    "parse_checksums",
    "parse_manifest",
    "parse_utc",
    "render_checksums",
    "sha256_hex",
    "utc_text",
]
