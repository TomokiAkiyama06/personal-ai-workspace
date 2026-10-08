#!/usr/bin/env python3
"""paw-release: versioned releases, manual update and rollback of the Personal AI
Workspace (PAW-068, Issue #54, Decision 0079).

Standard library only (Python 3.11+): it runs with the host's ``python3``, not
with the venv of a release it is replacing. What needs the database it asks the
server-local commands of a release (``<release>/venv/bin/python -m
paw_backend.cli deploy-*``, ``recovery-backup-run``; ``python -m alembic``).
Services are started and stopped only through the commands of the
configuration (``systemctl ...`` in production); this tool itself never calls
``systemctl`` or ``sudo``.

Layout (``root``, e.g. ``/opt/paw``)::

    releases/<version>/      one immutable release: the source of one commit
      release.json           (``git archive``), its venv and web build, and the
                             manifest (commit, schema head, migration chain)
    current -> releases/<version>   what the units run (switched atomically)

State (``state_dir``, e.g. ``/var/lib/paw/deploy``): ``state.json`` (the
known-good releases, the operation in progress), ``history.jsonl`` (every step
of every operation) and ``lock``.

Commands::

    build --repo DIR --commit REF [--version NAME]
    list | status
    precheck VERSION        read-only: the pre-update checks and the plan
    update VERSION [--stop-now]
    rollback [--to VERSION] [--restore-point LABEL]
    install VERSION         the first release on a server (no current one)
    end-maintenance         resume the tasks after a manual fix (exit 5 before)
    prune-restore-points    delete the restore points older than
                            restore_point_keep_days (also after every update)

Exit codes: ``0`` done; ``1`` refused (usage, a failed pre-update check, an
incompatible schema: nothing changed); ``2`` environment (configuration, state,
lock); ``3`` aborted before anything was switched (the current release runs
again and the tasks resumed); ``4`` the update failed and the known-good release
runs again; ``5`` the system is left in maintenance (a rollback failed or could
not be verified): the notify commands ran; fix it by hand, then
``end-maintenance``.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_ABORTED = 3
EXIT_ROLLED_BACK = 4
EXIT_MAINTENANCE = 5

MANIFEST = "release.json"
MANIFEST_FORMAT = 1
VERSION_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
MIGRATIONS = Path("apps/backend/migrations/versions")
BACKEND = Path("apps/backend")
# Decision 0079 4: a migration that says ``paw_compatibility = "expand"`` leaves
# a schema the release before it can run on. Anything else (or nothing) does not.
EXPAND = "expand"
COMPATIBILITIES = frozenset({EXPAND, "contract", "data"})
# The files the source digest leaves out: what the build adds.
_DIGEST_SKIP = (MANIFEST, "venv/", "apps/web/dist/", "apps/web/node_modules/")

COMMAND_KEYS = (
    "build",
    "precheck",
    "stop",
    "start",
    "health",
    "post_restore",
    "notify",
)


class ReleaseError(Exception):
    """A refusal or failure with a message for the operator (no secrets)."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


# -- running commands ------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], Path | None, Mapping[str, str], float], Result]


def subprocess_runner(
    argv: Sequence[str], cwd: Path | None, env: Mapping[str, str], timeout: float
) -> Result:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", "timed out")
    except OSError as error:
        return Result(127, "", f"cannot run ({type(error).__name__})")
    return Result(completed.returncode, completed.stdout, completed.stderr)


# -- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    restore_point_dir: Path
    env_file: Path | None
    min_free_bytes: int
    restore_point_keep_days: float
    drain_timeout_seconds: int
    health_timeout_seconds: int
    health_interval_seconds: float
    command_timeout_seconds: int
    python: str
    backend_prefix: tuple[str, ...]
    pg_dump: str
    pg_restore: str
    commands: Mapping[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current_link(self) -> Path:
        return self.root / "current"


def _absolute(data: Mapping[str, object], key: str, default: str | None) -> Path:
    value = data.get(key, default)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ReleaseError(f"config: {key} must be an absolute path", EXIT_ENVIRONMENT)
    return Path(value)


def _number(data: Mapping[str, object], key: str, default: float, low: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < low:
        raise ReleaseError(f"config: {key} must be a number >= {low}", EXIT_ENVIRONMENT)
    return value


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError("config: file not found", EXIT_ENVIRONMENT) from None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ReleaseError("config: not readable TOML", EXIT_ENVIRONMENT) from None
    raw = data.get("commands", {})
    if not isinstance(raw, dict) or set(raw) - set(COMMAND_KEYS):
        raise ReleaseError(
            f"config: [commands] takes only {', '.join(COMMAND_KEYS)}",
            EXIT_ENVIRONMENT,
        )
    commands: dict[str, tuple[tuple[str, ...], ...]] = {}
    for key in COMMAND_KEYS:
        entries = raw.get(key, [])
        if not isinstance(entries, list) or not all(
            isinstance(entry, list)
            and entry
            and all(isinstance(part, str) and part for part in entry)
            for entry in entries
        ):
            raise ReleaseError(
                f"config: commands.{key} must be a list of argv lists",
                EXIT_ENVIRONMENT,
            )
        commands[key] = tuple(tuple(entry) for entry in entries)
    env_file = data.get("env_file")
    for key in ("python", "pg_dump", "pg_restore"):
        if key in data and not (isinstance(data[key], str) and data[key]):
            raise ReleaseError(f"config: {key} must be a string", EXIT_ENVIRONMENT)
    python = data.get("python", "venv/bin/python")
    if Path(python).is_absolute() or ".." in Path(python).parts:
        raise ReleaseError(
            "config: python is a path inside the release", EXIT_ENVIRONMENT
        )
    return Config(
        root=_absolute(data, "root", None),
        state_dir=_absolute(data, "state_dir", None),
        restore_point_dir=_absolute(data, "restore_point_dir", None),
        env_file=_absolute(data, "env_file", None) if env_file is not None else None,
        min_free_bytes=int(_number(data, "min_free_gib", 5, 0) * 1024**3),
        restore_point_keep_days=_number(data, "restore_point_keep_days", 7, 1),
        drain_timeout_seconds=int(_number(data, "drain_timeout_seconds", 900, 1)),
        health_timeout_seconds=int(_number(data, "health_timeout_seconds", 300, 1)),
        health_interval_seconds=_number(data, "health_interval_seconds", 5, 0),
        command_timeout_seconds=int(_number(data, "command_timeout_seconds", 3600, 1)),
        python=python,
        backend_prefix=_prefix(data.get("backend_prefix", [])),
        pg_dump=data.get("pg_dump", "pg_dump"),
        pg_restore=data.get("pg_restore", "pg_restore"),
        commands=commands,
    )


def _prefix(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ReleaseError("config: backend_prefix is an argv list", EXIT_ENVIRONMENT)
    return tuple(value)


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines (systemd ``EnvironmentFile`` style). Refused unless
    only its owner may read it: it holds the migration role's credential."""
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ReleaseError("env_file not found", EXIT_ENVIRONMENT) from None
    if info.st_mode & 0o077:
        raise ReleaseError(
            "env_file must not be readable by group or others (chmod 600)",
            EXIT_ENVIRONMENT,
        )
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ReleaseError("env_file: a line is not KEY=VALUE", EXIT_ENVIRONMENT)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


# -- releases and their manifests -----------------------------------------------


@dataclass(frozen=True)
class Migration:
    revision: str
    down_revision: str | None
    compatibility: str | None


@dataclass(frozen=True)
class Release:
    version: str
    path: Path
    commit: str
    schema_head: str | None
    migrations: Mapping[str, Migration]
    source_digest: str

    def chain(self) -> list[str]:
        """The revisions from the head down to the base."""
        chain: list[str] = []
        revision = self.schema_head
        while revision is not None:
            chain.append(revision)
            revision = self.migrations[revision].down_revision
        return chain

    def path_between(self, newer: str, older: str | None) -> list[Migration] | None:
        """The migrations that lead from ``older`` up to ``newer`` (newest
        first), or ``None`` when ``older`` is not below ``newer`` here."""
        steps: list[Migration] = []
        revision: str | None = newer
        while revision != older:
            if revision is None or revision not in self.migrations:
                return None
            steps.append(self.migrations[revision])
            revision = self.migrations[revision].down_revision
        return steps


def _literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return _INVALID


_INVALID = object()


def read_migrations(directory: Path) -> dict[str, Migration]:
    """The module-level ``revision`` / ``down_revision`` /
    ``paw_compatibility`` of each Alembic version file (parsed, not run)."""
    migrations: dict[str, Migration] = {}
    if not directory.is_dir():
        return migrations
    for path in sorted(directory.glob("*.py")):
        values: dict[str, object] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name, value = node.targets[0].id, node.value
            else:
                continue
            if value is not None and name in (
                "revision",
                "down_revision",
                "paw_compatibility",
            ):
                values[name] = _literal(value)
        if "revision" not in values:
            continue
        revision, down = values["revision"], values.get("down_revision")
        compatibility = values.get("paw_compatibility")
        if (
            not isinstance(revision, str)
            or not (down is None or isinstance(down, str))
            or not (compatibility is None or compatibility in COMPATIBILITIES)
        ):
            raise ReleaseError(
                f"migration {path.name}: revision / down_revision must be single "
                f"strings and paw_compatibility one of {sorted(COMPATIBILITIES)}"
            )
        if revision in migrations:
            raise ReleaseError(f"migration {revision} is defined twice")
        migrations[revision] = Migration(revision, down, compatibility)
    return migrations


def schema_head(migrations: Mapping[str, Migration]) -> str | None:
    referenced = {m.down_revision for m in migrations.values()}
    heads = sorted(set(migrations) - referenced)
    if len(heads) > 1:
        raise ReleaseError(f"the migrations have {len(heads)} heads: {heads}")
    for migration in migrations.values():
        if migration.down_revision is not None and (
            migration.down_revision not in migrations
        ):
            raise ReleaseError(
                f"migration {migration.revision} revises an unknown revision"
            )
    return heads[0] if heads else None


def source_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    files = []
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if relative.startswith(_DIGEST_SKIP) or relative == MANIFEST:
            continue
        if path.is_symlink() or path.is_file():
            files.append((relative, path))
    for relative, path in sorted(files):
        digest.update(relative.encode() + b"\0")
        if path.is_symlink():
            digest.update(b"L" + os.readlink(path).encode())
        else:
            digest.update(b"F" + hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def load_release(config: Config, version: str, *, verify: bool = False) -> Release:
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ReleaseError("invalid release name")
    path = config.releases / version
    try:
        data = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError(f"release {version} not found") from None
    except (OSError, ValueError):
        raise ReleaseError(f"release {version}: manifest not readable") from None
    if data.get("format") != MANIFEST_FORMAT or data.get("version") != version:
        raise ReleaseError(f"release {version}: manifest not valid")
    migrations = {
        entry["revision"]: Migration(
            entry["revision"], entry["down_revision"], entry["compatibility"]
        )
        for entry in data["migrations"]
    }
    release = Release(
        version=version,
        path=path,
        commit=data["commit"],
        schema_head=data["schema_head"],
        migrations=migrations,
        source_digest=data["source_digest"],
    )
    if verify and source_digest(path) != release.source_digest:
        raise ReleaseError(
            f"release {version}: its files differ from when it was built "
            "(source digest); build a new release"
        )
    return release


def list_versions(config: Config) -> list[str]:
    if not config.releases.is_dir():
        return []
    return sorted(
        entry.name
        for entry in config.releases.iterdir()
        if (entry / MANIFEST).is_file() and re.fullmatch(VERSION_PATTERN, entry.name)
    )


def current_version(config: Config) -> str | None:
    link = config.current_link
    if not link.is_symlink():
        if link.exists():
            raise ReleaseError("current is not a symlink", EXIT_ENVIRONMENT)
        return None
    target = Path(os.readlink(link))
    if target.parent.name != "releases" or not re.fullmatch(
        VERSION_PATTERN, target.name
    ):
        raise ReleaseError("current points outside releases/", EXIT_ENVIRONMENT)
    return target.name


def switch_current(config: Config, version: str) -> None:
    """Point ``current`` at the release atomically (a new link, renamed)."""
    temporary = config.root / ".current.new"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    os.symlink(Path("releases") / version, temporary)
    os.replace(temporary, config.current_link)


# -- state, history and the lock -------------------------------------------------


class State:
    def __init__(self, config: Config) -> None:
        self._directory = config.state_dir
        self._path = config.state_dir / "state.json"
        self._history = config.state_dir / "history.jsonl"

    def ensure(self) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def read(self) -> dict[str, object]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"known_good": [], "in_progress": None}
        except (OSError, ValueError):
            raise ReleaseError("state.json is not readable", EXIT_ENVIRONMENT) from None
        data.setdefault("known_good", [])
        data.setdefault("in_progress", None)
        return data

    def write(self, data: Mapping[str, object]) -> None:
        temporary = self._path.with_name(".state.json.tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self._path)

    def update(self, **changes: object) -> dict[str, object]:
        data = self.read()
        data.update(changes)
        self.write(data)
        return data

    def mark_known_good(self, version: str) -> None:
        data = self.read()
        known = [v for v in data["known_good"] if v != version] + [version]
        data["known_good"] = known
        self.write(data)

    def log(self, operation: str, step: str, outcome: str, **details: object) -> None:
        entry = {
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "operation": operation,
            "step": step,
            "outcome": outcome,
            **details,
        }
        with self._history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        with (self._directory / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError(
                    "another paw-release operation is running", EXIT_ENVIRONMENT
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


# -- the tool --------------------------------------------------------------------


@dataclass
class Plan:
    """What an update to ``target`` needs."""

    current: str | None
    target: str
    revision: str | None
    migration: bool
    checks: list[dict[str, object]]

    @property
    def ok(self) -> bool:
        return all(check["ok"] for check in self.checks)


class Tool:
    def __init__(
        self,
        config: Config,
        *,
        runner: Runner = subprocess_runner,
        out: io.TextIOBase | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.config = config
        self.state = State(config)
        self._runner = runner
        self._out = out if out is not None else sys.stderr
        self._sleep = sleep
        self._monotonic = monotonic
        self._free_bytes = free_bytes or _free_bytes
        self._backend_env: dict[str, str] | None = None

    # -- output and commands ---------------------------------------------------

    def say(self, message: str) -> None:
        print(message, file=self._out)

    def backend_env(self) -> dict[str, str]:
        if self._backend_env is None:
            env = dict(os.environ)
            if self.config.env_file is not None:
                env.update(read_env_file(self.config.env_file))
            self._backend_env = env
        return self._backend_env

    def backend(self, release: Release, *args: str) -> Result:
        """``<release>/<python> -m <args>`` in its ``apps/backend``."""
        return self._runner(
            [
                *self.config.backend_prefix,
                str(release.path / self.config.python),
                "-m",
                *args,
            ],
            release.path / BACKEND,
            self.backend_env(),
            self.config.command_timeout_seconds,
        )

    def cli(self, release: Release, *args: str) -> Result:
        return self.backend(release, "paw_backend.cli", *args)

    def post_restore(self, release: Release) -> Result:
        """The backend commands of ``post_restore`` (e.g. ``user-erasure-run``)
        with the release that runs on the restored database."""
        for args in self.config.commands.get("post_restore", ()):
            result = self.cli(release, *args)
            if result.code != 0:
                return Result(result.code, "", f"{args[0]}: {result.code}")
        return Result(0)

    def configured(
        self, key: str, *, release: Release | None = None, message: str = ""
    ) -> Result:
        """Run the configured commands of ``key`` in order; the first failure."""
        for argv in self.config.commands.get(key, ()):
            argv = [
                part.replace("{release}", str(release.path) if release else "")
                .replace("{version}", release.version if release else "")
                .replace("{message}", message)
                for part in argv
            ]
            result = self._runner(
                argv, None, dict(os.environ), self.config.command_timeout_seconds
            )
            if result.code != 0:
                return Result(result.code, result.stdout, f"{argv[0]}: {result.code}")
        return Result(0)

    # -- build -----------------------------------------------------------------

    def build(self, repo: Path, commit: str, version: str | None) -> Release:
        git = ["git", "-C", str(repo)]
        # Not the GIT_DIR / GIT_INDEX_FILE of a calling git (a hook): the repo's.
        git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        resolved = self._runner(
            [*git, "rev-parse", "--verify", f"{commit}^{{commit}}"],
            None,
            git_env,
            60,
        )
        if resolved.code != 0:
            raise ReleaseError("build: the commit was not found")
        sha = resolved.stdout.strip()
        if version is None:
            date = self._runner(
                [*git, "show", "-s", "--format=%cd", "--date=format:%Y%m%d", sha],
                None,
                git_env,
                60,
            )
            version = f"{date.stdout.strip()}-{sha[:12]}"
        if not re.fullmatch(VERSION_PATTERN, version):
            raise ReleaseError("build: invalid release name")
        final = self.config.releases / version
        if final.exists():
            raise ReleaseError(
                f"build: release {version} exists (releases are immutable)"
            )
        self.config.releases.mkdir(parents=True, exist_ok=True)
        staging = self.config.releases / f".build-{version}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o755)
        try:
            archive = subprocess.run(
                [*git, "archive", "--format=tar", sha],
                env=git_env,
                capture_output=True,
                check=False,
                timeout=600,
            )
            if archive.returncode != 0:
                raise ReleaseError("build: git archive failed", EXIT_ABORTED)
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(staging, filter="data")
            migrations = read_migrations(staging / MIGRATIONS)
            manifest = {
                "format": MANIFEST_FORMAT,
                "version": version,
                "commit": sha,
                "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "schema_head": schema_head(migrations),
                "migrations": [
                    {
                        "revision": m.revision,
                        "down_revision": m.down_revision,
                        "compatibility": m.compatibility,
                    }
                    for m in sorted(migrations.values(), key=lambda m: m.revision)
                ],
                "source_digest": source_digest(staging),
            }
            (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
            for argv in self.config.commands.get("build", ()):
                argv = [
                    part.replace("{release}", str(staging)).replace(
                        "{version}", version
                    )
                    for part in argv
                ]
                result = self._runner(
                    argv, staging, dict(os.environ), self.config.command_timeout_seconds
                )
                if result.code != 0:
                    raise ReleaseError(
                        f"build: {argv[0]} failed ({result.code})", EXIT_ABORTED
                    )
            os.replace(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.say(f"Built release {version} (commit {sha[:12]}).")
        return load_release(self.config, version)

    # -- the pre-update check ---------------------------------------------------

    def plan(self, target_version: str) -> Plan:
        checks: list[dict[str, object]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        current_name = current_version(self.config)
        if current_name is None:
            raise ReleaseError("no current release: use install for the first one")
        target = load_release(self.config, target_version, verify=True)
        check("target_release", True, f"commit {target.commit[:12]}")
        check(
            "target_differs",
            target.version != current_name,
            "a new release" if target.version != current_name else "already current",
        )
        in_progress = self.state.read()["in_progress"]
        check(
            "no_operation_in_progress",
            in_progress is None,
            "none" if in_progress is None else f"{in_progress} (roll back first)",
        )
        current = load_release(self.config, current_name, verify=True)
        for name, path in (
            ("disk_free_releases", self.config.root),
            ("disk_free_restore_points", self.config.restore_point_dir),
        ):
            free = self._free_bytes(path)
            check(
                name,
                free >= self.config.min_free_bytes,
                f"{free // 1024**3} GiB free (minimum "
                f"{self.config.min_free_bytes // 1024**3} GiB)",
            )
        result = self.cli(current, "deploy-precheck")
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = None
        revision = None
        if not isinstance(report, dict):
            check("database_precheck", False, f"deploy-precheck failed ({result.code})")
        else:
            for entry in report.get("checks", []):
                check(f"db:{entry['name']}", bool(entry["ok"]), str(entry["detail"]))
            revision = report.get("status", {}).get("schema_revision")
        migration = False
        if revision is not None:
            if revision == target.schema_head:
                check("schema", True, f"{revision}: no migration")
            elif target.schema_head and target.path_between(
                target.schema_head, revision
            ):
                migration = True
                check(
                    "schema",
                    True,
                    f"{revision} -> {target.schema_head}: migration (a restore "
                    "point is taken and verified first)",
                )
            else:
                steps = self.steps_between(revision, target.schema_head, current)
                compatible = bool(steps) and all(
                    step.compatibility == EXPAND for step in steps
                )
                check(
                    "schema",
                    compatible,
                    f"the database ({revision}) is newer than the target "
                    f"({target.schema_head}); "
                    + (
                        "only expand migrations in between: the target runs on it"
                        if compatible
                        else "not backward compatible: roll back with a restore point"
                    ),
                )
        failed = self.configured("precheck", release=target)
        check(
            "configured_checks",
            failed.code == 0,
            "passed" if failed.code == 0 else f"failed: {failed.stderr}",
        )
        return Plan(current_name, target.version, revision, migration, checks)

    def steps_between(
        self, newer: str, older: str | None, first: Release
    ) -> list[Migration] | None:
        """The migrations from ``older`` up to ``newer``, read from the first
        release whose manifest knows ``newer``: ``first``, else any built one
        (after an application-only rollback the database is newer than the
        current release, Codex review #204)."""
        candidates = [first] + [
            v for v in list_versions(self.config) if v != first.version
        ]
        for candidate in candidates:
            release = (
                candidate
                if isinstance(candidate, Release)
                else load_release(self.config, candidate)
            )
            if newer in release.migrations:
                return release.path_between(newer, older)
        return None

    def show_plan(self, plan: Plan) -> None:
        for entry in plan.checks:
            mark = "ok  " if entry["ok"] else "FAIL"
            self.say(f"  [{mark}] {entry['name']}: {entry['detail']}")
        self.say(
            f"Update {plan.current} -> {plan.target}: "
            + ("schema migration" if plan.migration else "application only")
        )

    # -- update ----------------------------------------------------------------

    def update(self, target_version: str, *, stop_now: bool = False) -> int:
        with self.state.lock():
            plan = self.plan(target_version)
            self.show_plan(plan)
            if not plan.ok:
                self.say("Refused: a pre-update check failed. Nothing was changed.")
                return EXIT_REFUSED
            current = load_release(self.config, plan.current)
            target = load_release(self.config, plan.target)
            return _Update(self, current, target, plan, stop_now).run()

    # -- rollback --------------------------------------------------------------

    def rollback(self, to: str | None, restore_point: str | None) -> int:
        with self.state.lock():
            current_name = current_version(self.config)
            if current_name is None:
                raise ReleaseError("no current release")
            known_good = list(self.state.read()["known_good"])
            if to is None:
                # The one that became known-good just before the current one
                # (after a rollback, never forward to the release left).
                earlier = (
                    known_good[: known_good.index(current_name)]
                    if current_name in known_good
                    else known_good
                )
                if not earlier:
                    raise ReleaseError("no earlier known-good release to roll back to")
                to = earlier[-1]
            if to not in known_good:
                raise ReleaseError(
                    f"{to} is not a known-good release (it never passed an update)"
                )
            if to == current_name:
                raise ReleaseError(f"{to} is already current")
            current = load_release(self.config, current_name)
            target = load_release(self.config, to, verify=True)
            # A current release whose files changed runs none of its code: the
            # target's commands stand in for it (Codex review #204).
            try:
                load_release(self.config, current_name, verify=True)
                tools = current
            except ReleaseError:
                self.say(f"  {current_name} changed since it was built: not run")
                tools = target
            status = (self._status(current) if tools is current else None) or (
                self._status(target)
            )
            revision = status.get("schema_revision") if status else None
            if restore_point is not None:
                point = self._restore_point(restore_point)
                if not point.get("verified"):
                    raise ReleaseError(f"restore point {restore_point} is not verified")
                if point.get("revision") != target.schema_head:
                    raise ReleaseError(
                        f"restore point {restore_point} is at revision "
                        f"{point.get('revision')}, {to} needs {target.schema_head}"
                    )
            else:
                if revision is None:
                    raise ReleaseError(
                        "the schema revision is unknown: give --restore-point"
                    )
                if revision != target.schema_head:
                    steps = self.steps_between(revision, target.schema_head, current)
                    if not steps or any(s.compatibility != EXPAND for s in steps):
                        raise ReleaseError(
                            f"the database ({revision}) is not backward compatible "
                            f"with {to} ({target.schema_head}): give --restore-point "
                            "(a verified point at that revision)"
                        )
            self.say(
                f"Rollback {current_name} -> {to}"
                + (f" with restore point {restore_point}" if restore_point else "")
            )
            return _Rollback(self, current, target, restore_point, status, tools).run()

    def _restore_point(self, label: str) -> dict:
        if not re.fullmatch(VERSION_PATTERN, label):
            raise ReleaseError("invalid restore point label")
        path = self.config.restore_point_dir / f"{label}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ReleaseError(f"restore point {label} not found") from None
        except (OSError, ValueError):
            raise ReleaseError(f"restore point {label} is not readable") from None
        if not isinstance(data, dict):
            raise ReleaseError(f"restore point {label} is not readable")
        return data

    def _status(self, release: Release) -> dict | None:
        result = self.cli(release, "deploy-status")
        if result.code != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            return None

    # -- install ---------------------------------------------------------------

    def install(self, version: str) -> int:
        with self.state.lock():
            if current_version(self.config) is not None:
                raise ReleaseError("a release is installed already: use update")
            target = load_release(self.config, version, verify=True)
            self.state.update(in_progress={"operation": "install", "to": version})
            self.state.log("install", "started", "ok", to=version)
            result = self.backend(target, "alembic", "upgrade", "head")
            if result.code != 0:
                self.state.log("install", "migrate", "failed", code=result.code)
                self.say("FAILED: alembic upgrade head failed; nothing was started.")
                return EXIT_ABORTED
            switch_current(self.config, version)
            started = self.configured("start", release=target)
            healthy = started.code == 0 and self.healthy(target, target.schema_head)
            if not healthy:
                self.state.log("install", "health", "failed")
                self.say("FAILED: the release did not become healthy.")
                return EXIT_MAINTENANCE
            self.state.mark_known_good(version)
            self.state.update(in_progress=None)
            self.state.log("install", "finished", "ok", to=version)
            self.say(f"Installed {version}.")
            return EXIT_OK

    # -- end-maintenance -------------------------------------------------------

    def end_maintenance(self) -> int:
        with self.state.lock():
            current_name = current_version(self.config)
            if current_name is None:
                raise ReleaseError("no current release")
            current = load_release(self.config, current_name, verify=True)
            result = self.cli(current, "deploy-maintenance-end")
            if result.code != 0:
                self.say(f"FAILED: deploy-maintenance-end ({result.code}).")
                return EXIT_MAINTENANCE
            self.state.update(in_progress=None)
            self.state.log("end-maintenance", "finished", "ok", release=current_name)
            self.say("Maintenance ended: the queue runs and held tasks resumed.")
            return EXIT_OK

    # -- restore points --------------------------------------------------------

    def prune_restore_points(self) -> list[str]:
        """Delete the restore points older than ``restore_point_keep_days``
        (Decision 0079 5: a dump keeps the personal data of users deleted since,
        so it must not outlive the erasure). The point of an operation in
        progress is kept. The labels deleted."""
        directory = self.config.restore_point_dir
        if not directory.is_dir():
            return []
        in_progress = self.state.read()["in_progress"] or {}
        keep = in_progress.get("restore_point")
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=self.config.restore_point_keep_days
        )
        deleted = []
        for metadata in sorted(directory.glob("*.json")):
            label = metadata.stem
            if label == keep:
                continue
            try:
                created = dt.datetime.fromisoformat(
                    json.loads(metadata.read_text(encoding="utf-8"))["created_at"]
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.UTC)
            except (OSError, ValueError, KeyError, TypeError):
                created = dt.datetime.fromtimestamp(metadata.stat().st_mtime, dt.UTC)
            if created < cutoff:
                with contextlib.suppress(FileNotFoundError):
                    metadata.with_suffix(".dump").unlink()
                metadata.unlink()
                deleted.append(label)
        if deleted:
            self.state.log("prune", "restore_points", "ok", deleted=deleted)
        return deleted

    # -- health ----------------------------------------------------------------

    def healthy(self, release: Release, revision: str | None) -> bool:
        """The release's schema revision is ``revision`` and the configured
        health commands pass, within ``health_timeout_seconds``."""
        deadline = self._monotonic() + self.config.health_timeout_seconds
        while True:
            status = self._status(release)
            if (
                status is not None
                and status.get("schema_revision") == revision
                and self.configured("health", release=release).code == 0
            ):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(self.config.health_interval_seconds)


def _free_bytes(path: Path) -> int:
    while not path.exists():
        path = path.parent
    usage = os.statvfs(path)
    return usage.f_bavail * usage.f_frsize


class _Operation:
    """The steps shared by an update and a rollback."""

    name = "operation"

    def __init__(self, tool: Tool) -> None:
        self.tool = tool
        self.state = tool.state
        self.say = tool.say

    def step(self, step: str, result: Result, **details: object) -> bool:
        ok = result.code == 0
        self.state.log(
            self.name,
            step,
            "ok" if ok else "failed",
            **({} if ok else {"code": result.code}),
            **details,
        )
        self.state.update(
            in_progress={**(self.state.read()["in_progress"] or {}), "stage": step}
        )
        if not ok:
            self.say(f"  {step}: FAILED ({result.code})")
        else:
            self.say(f"  {step}: ok")
        return ok

    def maintenance_mode(self, reason: str) -> int:
        """Leave everything stopped and in maintenance, and tell the people."""
        message = (
            f"Personal AI Workspace: {self.name} FAILED ({reason}); the system is "
            "in maintenance. See paw-release status and history.jsonl."
        )
        self.tool.configured("notify", message=message)
        self.state.log(self.name, "maintenance", "left", reason=reason)
        self.say(
            f"FAILED: {reason}. The system stays in maintenance (no task starts); "
            "fix it, then run end-maintenance (or rollback)."
        )
        return EXIT_MAINTENANCE


class _Update(_Operation):
    name = "update"

    def __init__(self, tool, current, target, plan, stop_now) -> None:
        super().__init__(tool)
        self.current = current
        self.target = target
        self.plan = plan
        self.stop_now = stop_now
        self.label = (
            f"{current.version}-to-{target.version}-"
            f"{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S}"
        )[:64]
        self.restore_point: str | None = None
        self.migrated = False

    def run(self) -> int:
        tool, current, target = self.tool, self.current, self.target
        self.state.update(
            in_progress={
                "operation": "update",
                "from": current.version,
                "to": target.version,
                "migration": self.plan.migration,
            }
        )
        self.state.log("update", "started", "ok", **{"from": current.version})
        # 1. No task starts; the running ones are held and drain.
        if not self.step(
            "maintenance",
            tool.cli(
                current,
                "deploy-maintenance-begin",
                "--from-release",
                current.version,
                "--to-release",
                target.version,
            ),
        ):
            # The row may be committed although the command failed (holding a
            # task, the audit row): ending it is idempotent and lets the queue go.
            return self.abort(running=True)
        drained = self.step(
            "drain",
            tool.cli(
                current,
                "deploy-drain",
                "--timeout-seconds",
                str(tool.config.drain_timeout_seconds),
            ),
        )
        if not drained and not self.stop_now:
            return self.abort(running=True)
        if not drained:
            self.say("  --stop-now: going on; the nodes still running are cut off")
        # 2. The Recovery Repository is brought up to date.
        if not self.step("recovery_backup", tool.cli(current, "recovery-backup-run")):
            return self.abort(running=True)
        # 3. Every writer stops (the backend, its timers).
        if not self.step("stop", tool.configured("stop", release=current)):
            return self.abort(running=False)
        # 4. Before a migration: a verified restore point.
        if self.plan.migration:
            point = ["--dir", str(tool.config.restore_point_dir), "--label", self.label]
            tools = [
                "--pg-dump",
                tool.config.pg_dump,
                "--pg-restore",
                tool.config.pg_restore,
            ]
            if not self.step(
                "restore_point",
                tool.cli(current, "deploy-restore-point-create", *point, *tools),
                label=self.label,
            ) or not self.step(
                "restore_point_verify",
                tool.cli(current, "deploy-restore-point-verify", *point, *tools),
                label=self.label,
            ):
                return self.abort(running=False)
            self.restore_point = self.label
            self.state.update(
                in_progress={
                    **self.state.read()["in_progress"],
                    "restore_point": self.label,
                }
            )
            # 5. The migration (from here on a failure restores the point).
            self.migrated = True
            if not self.step(
                "migrate", tool.backend(target, "alembic", "upgrade", "head")
            ):
                return self.rollback()
        # 6. Switch and start the new release, then check it.
        switch_current(tool.config, target.version)
        self.state.log("update", "switch", "ok", to=target.version)
        if not self.step("start", tool.configured("start", release=target)):
            return self.rollback()
        expected = target.schema_head if self.plan.migration else self.plan.revision
        if not tool.healthy(target, expected):
            self.state.log("update", "health", "failed")
            self.say("  health: FAILED")
            return self.rollback()
        self.state.log("update", "health", "ok")
        self.state.mark_known_good(target.version)
        if not self.step("resume", tool.cli(target, "deploy-maintenance-end")):
            return self.maintenance_mode("the tasks could not be resumed")
        self.state.update(in_progress=None)
        self.state.log("update", "finished", "ok", to=target.version)
        tool.prune_restore_points()
        self.say(f"Updated to {target.version}.")
        return EXIT_OK

    def abort(self, *, running: bool) -> int:
        """Nothing was switched: the current release runs on (``running``: it
        was not stopped)."""
        tool = self.tool
        if not running and not self.step(
            "restart", tool.configured("start", release=self.current)
        ):
            return self.maintenance_mode("the current release did not start again")
        if not self.step("resume", tool.cli(self.current, "deploy-maintenance-end")):
            return self.maintenance_mode("the tasks could not be resumed")
        self.state.update(in_progress=None)
        self.state.log("update", "aborted", "ok")
        self.say(f"Aborted: {self.current.version} runs on; nothing was switched.")
        return EXIT_ABORTED

    def rollback(self) -> int:
        """After the migration or the switch: back to the known-good release."""
        tool, current = self.tool, self.current
        self.say(f"Rolling back to {current.version} ...")
        # The new release may still run (and write): nothing is restored or
        # switched unless it stopped.
        if not self.step("rollback_stop", tool.configured("stop", release=self.target)):
            return self.maintenance_mode(f"{self.target.version} could not be stopped")
        if self.migrated:
            point = [
                "--dir",
                str(tool.config.restore_point_dir),
                "--label",
                self.restore_point or "",
            ]
            if not self.step(
                "rollback_restore",
                tool.cli(
                    current,
                    "deploy-restore-point-restore",
                    *point,
                    "--pg-dump",
                    tool.config.pg_dump,
                    "--pg-restore",
                    tool.config.pg_restore,
                ),
            ):
                return self.maintenance_mode("the restore point could not be restored")
        switch_current(tool.config, current.version)
        self.state.log("update", "rollback_switch", "ok", to=current.version)
        if self.migrated and not self.step("post_restore", tool.post_restore(current)):
            return self.maintenance_mode("the post-restore commands failed")
        if not self.step("rollback_start", tool.configured("start", release=current)):
            return self.maintenance_mode("the known-good release did not start")
        if not tool.healthy(current, self.plan.revision):
            self.state.log("update", "rollback_health", "failed")
            return self.maintenance_mode("the known-good release is not healthy")
        if not self.step("resume", tool.cli(current, "deploy-maintenance-end")):
            return self.maintenance_mode("the tasks could not be resumed")
        self.state.update(in_progress=None)
        self.state.log("update", "rolled_back", "ok", to=current.version)
        self.say(f"The update failed; {current.version} runs again.")
        return EXIT_ROLLED_BACK


class _Rollback(_Operation):
    name = "rollback"

    def __init__(self, tool, current, target, restore_point, status, tools) -> None:
        super().__init__(tool)
        self.current = current
        # Whose backend commands run before the switch: the current release, or
        # the target when the current one's files changed.
        self.tools = tools
        self.target = target
        self.restore_point = restore_point
        self.status = status

    def run(self) -> int:
        tool, current, target = self.tool, self.current, self.target
        self.state.update(
            in_progress={
                "operation": "rollback",
                "from": current.version,
                "to": target.version,
                "restore_point": self.restore_point,
            }
        )
        self.state.log("rollback", "started", "ok", **{"from": current.version})
        # The current release may be broken: its maintenance and drain are tried,
        # the rollback goes on without them.
        if self.status is not None and self.status.get("maintenance") is None:
            self.step(
                "maintenance",
                tool.cli(
                    self.tools,
                    "deploy-maintenance-begin",
                    "--from-release",
                    current.version,
                    "--to-release",
                    target.version,
                ),
            )
            self.step(
                "drain",
                tool.cli(
                    self.tools,
                    "deploy-drain",
                    "--timeout-seconds",
                    str(tool.config.drain_timeout_seconds),
                ),
            )
        if not self.step("stop", tool.configured("stop", release=current)):
            return self.maintenance_mode(f"{current.version} could not be stopped")
        expected = target.schema_head
        if self.restore_point is not None:
            if not self.step(
                "restore",
                tool.cli(
                    target,
                    "deploy-restore-point-restore",
                    "--dir",
                    str(tool.config.restore_point_dir),
                    "--label",
                    self.restore_point,
                    "--pg-dump",
                    tool.config.pg_dump,
                    "--pg-restore",
                    tool.config.pg_restore,
                ),
            ):
                return self.maintenance_mode("the restore point could not be restored")
        elif self.status is not None:
            expected = self.status.get("schema_revision")
        switch_current(tool.config, target.version)
        self.state.log("rollback", "switch", "ok", to=target.version)
        if self.restore_point is not None and not self.step(
            "post_restore", tool.post_restore(target)
        ):
            return self.maintenance_mode("the post-restore commands failed")
        if not self.step("start", tool.configured("start", release=target)):
            return self.maintenance_mode(f"{target.version} did not start")
        if not tool.healthy(target, expected):
            self.state.log("rollback", "health", "failed")
            return self.maintenance_mode(f"{target.version} is not healthy")
        if not self.step("resume", tool.cli(target, "deploy-maintenance-end")):
            return self.maintenance_mode("the tasks could not be resumed")
        self.state.update(in_progress=None)
        self.state.log("rollback", "finished", "ok", to=target.version)
        tool.prune_restore_points()
        self.say(f"Rolled back to {target.version}.")
        return EXIT_OK


# -- command line ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paw-release",
        description="Versioned releases, update and rollback (Decision 0079).",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PAW_RELEASE_CONFIG", "/etc/paw/release.toml")),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", allow_abbrev=False)
    build.add_argument("--repo", type=Path, required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--version")
    commands.add_parser("list", allow_abbrev=False)
    commands.add_parser("status", allow_abbrev=False)
    precheck = commands.add_parser("precheck", allow_abbrev=False)
    precheck.add_argument("version")
    update = commands.add_parser("update", allow_abbrev=False)
    update.add_argument("version")
    update.add_argument(
        "--stop-now",
        action="store_true",
        help="go on when the drain times out (a critical security update)",
    )
    rollback = commands.add_parser("rollback", allow_abbrev=False)
    rollback.add_argument("--to")
    rollback.add_argument("--restore-point")
    install = commands.add_parser("install", allow_abbrev=False)
    install.add_argument("version")
    commands.add_parser("end-maintenance", allow_abbrev=False)
    commands.add_parser("prune-restore-points", allow_abbrev=False)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = subprocess_runner,
    out: io.TextIOBase | None = None,
    **tool_options: object,
) -> int:
    stream = out if out is not None else sys.stderr
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_REFUSED
    try:
        config = load_config(arguments.config)
        tool = Tool(config, runner=runner, out=stream, **tool_options)
        command = arguments.command
        if command == "build":
            with tool.state.lock():
                tool.build(arguments.repo, arguments.commit, arguments.version)
            return EXIT_OK
        if command in ("list", "status"):
            return _show(tool, verbose=command == "status")
        if command == "precheck":
            plan = tool.plan(arguments.version)
            tool.show_plan(plan)
            return EXIT_OK if plan.ok else EXIT_REFUSED
        if command == "update":
            return tool.update(arguments.version, stop_now=arguments.stop_now)
        if command == "rollback":
            return tool.rollback(arguments.to, arguments.restore_point)
        if command == "install":
            return tool.install(arguments.version)
        if command == "prune-restore-points":
            with tool.state.lock():
                deleted = tool.prune_restore_points()
            tool.say(f"Deleted {len(deleted)} restore point(s).")
            return EXIT_OK
        return tool.end_maintenance()
    except ReleaseError as error:
        print(f"paw-release: {error}", file=stream)
        return error.code
    except OSError as error:
        print(f"paw-release: file error ({type(error).__name__})", file=stream)
        return EXIT_ENVIRONMENT


def _show(tool: Tool, *, verbose: bool) -> int:
    current = current_version(tool.config)
    state = tool.state.read()
    known = state["known_good"]
    for version in list_versions(tool.config):
        marks = [
            m
            for m, on in (
                ("current", version == current),
                ("known-good", version in known),
            )
            if on
        ]
        tool.say(f"{version}" + (f"  ({', '.join(marks)})" if marks else ""))
    if verbose:
        tool.say(f"in progress: {state['in_progress'] or 'none'}")
        if current is not None:
            status = tool._status(load_release(tool.config, current))
            tool.say(
                "database: "
                + (json.dumps(status, sort_keys=True) if status else "unavailable")
            )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
