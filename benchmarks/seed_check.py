"""Hidden-check helper for the seed benchmark dataset (PAW-016, Decision 0041).

The evaluator starts this program through ``TestRunner.run_hidden``; the current
directory is the candidate worktree.  The program never writes into that worktree:

``unittest``
    Copies the worktree (without ``.git``) into a private temporary directory,
    lays the private overlay files (hidden tests, fixtures) over the copy, and runs
    ``python -m unittest`` there.  The check fails when a test fails, when no test
    ran, or, unless ``--allow-skips`` is given, when a test was skipped: a skipped
    PostgreSQL test must not turn a hidden check into a silent pass.  With
    ``--postgres-image`` a throwaway PostgreSQL container of that image is started
    for this check alone, its URL is handed to the tests as
    ``PAW_TEST_DATABASE_URL`` and the container is removed afterwards.  No URL is
    ever printed.
    ``--expect fail`` inverts the verdict (used to check that a test the candidate
    repaired still detects a known bug).

``forbidden-changes``
    Fails when the worktree differs from ``--base`` outside the ``--allow``
    paths (an exact path, or a directory ending with ``/``) (committed, staged, unstaged and untracked files).  The files
    are hashed from their bytes; the candidate's index flags, ignore rules and
    attributes are not trusted.

The overlay directory, commands and base URL file are evaluator-private material
(see ``benchmarks/seed_dataset.py``); this module holds none of them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".venv", "*.pyc")
_RAN = re.compile(rb"^Ran (\d+) tests? in ", re.MULTILINE)
_SKIPPED = re.compile(rb"skipped=(\d+)")
_SUMMARY = re.compile(rb"^(OK|FAILED)\b.*$", re.MULTILINE)
_FAILED_COUNTS = re.compile(rb"^FAILED \(([^)]*)\)\s*$", re.MULTILINE)
_MAX_ECHO = 48 * 1024
# Only the end of the unittest output is kept while it streams (the summary is at
# the end), so a test that prints without bound cannot exhaust the evaluator.
_MAX_CAPTURE = 1024 * 1024


class _Terminated(Exception):
    pass


def _on_term(signum, frame):  # pragma: no cover - exercised by TestRunner timeouts
    raise _Terminated()


def _echo(data: bytes) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout.flush()
        buffer.write(data)
        buffer.flush()
    else:  # redirected to a text stream (tests)
        sys.stdout.write(data.decode(errors="replace"))


def _copy_tree(worktree: Path, destination: Path) -> None:
    shutil.copytree(worktree, destination, symlinks=True, ignore=_IGNORED)


def _real_directory(destination: Path, relative: Path) -> Path:
    """Return ``destination / relative`` as a real directory inside the copy.

    Every existing component is checked without following links: a candidate
    symlink (or file) in the way is removed from the private copy, so a later
    write can never leave the temporary tree through a linked parent.
    """
    current = destination
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            current.unlink()
        if not current.exists():
            current.mkdir()
    return current


def _apply_overlay(overlay: Path, destination: Path) -> int:
    count = 0
    for source in sorted(overlay.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(overlay)
        parent = _real_directory(destination, relative.parent)
        target = parent / relative.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copyfile(source, target, follow_symlinks=False)
        count += 1
    return count


@dataclass(frozen=True)
class _Cluster:
    """A PostgreSQL cluster (a container) that exists for one hidden check only."""

    container: str
    test_url: str


_CLUSTER_READY_SECONDS = 120.0


def _docker(*arguments: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _start_cluster(image: str) -> _Cluster:
    """Start a throwaway PostgreSQL container for one check and return its URL.

    The tests get the superuser of a cluster nobody else uses: the migrations under
    test create and drop the ``vector`` extension (pgvector is not a trusted
    extension) and the grant tests create roles, so the login cannot be narrowed.
    Whatever the tests do to that cluster (extra roles or databases, a changed
    password) disappears with the container, and no later check or task shares it
    (Decision 0041).  The data directory is a tmpfs; the port is published on
    127.0.0.1 only; the password is random and passed through a private env file,
    not on a command line.
    """
    container = f"paw-seed-{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(32)
    directory = Path(tempfile.mkdtemp(prefix="paw-seed-pg-"))
    try:
        env_file = directory / "env"
        descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"POSTGRES_PASSWORD={password}\n")
        # From here on a container may exist (even when ``docker run`` times out or
        # fails half-way), so every failure removes it before re-raising.
        try:
            started = _docker(
                "run",
                "--detach",
                "--rm",
                "--name",
                container,
                "--env-file",
                str(env_file),
                "--tmpfs",
                "/var/lib/postgresql",
                "--publish",
                "127.0.0.1::5432",
                image,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        if started.returncode != 0:
            raise RuntimeError("could not start the PostgreSQL container")
        port = _docker("port", container, "5432/tcp").stdout.decode().split(":")[-1]
        test_url = (
            f"postgresql://postgres:{password}@127.0.0.1:{int(port.strip())}/postgres"
        )
        _wait_until_ready(test_url)
    except BaseException:
        _remove_container(container)
        raise
    return _Cluster(container, test_url)


def _wait_until_ready(url: str) -> None:
    """Wait until the final server (not the image's init-time server) accepts TCP."""
    import psycopg

    deadline = time.monotonic() + _CLUSTER_READY_SECONDS
    while True:
        try:
            with psycopg.connect(url, connect_timeout=5) as connection:
                connection.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise RuntimeError("the PostgreSQL container did not become ready")
            time.sleep(0.5)


def _remove_container(container: str) -> bool:
    """Remove a check's container and its anonymous volumes; True when removed."""
    try:
        removed = _docker("rm", "--force", "--volumes", container)
    except (OSError, subprocess.SubprocessError):
        return False
    return removed.returncode == 0


def _stop_cluster(cluster: _Cluster) -> bool:
    """Remove the check's container; False (and a message without the URL) on failure."""
    if not _remove_container(cluster.container):
        print("seed_check: could not remove the PostgreSQL container; the check fails")
        return False
    return True


def _only_assertion_failures(output: bytes) -> bool:
    """True when the last unittest summary reports failures and no errors."""
    found = _FAILED_COUNTS.findall(output)
    if not found:
        return False
    counts = {}
    for part in found[-1].split(b","):
        key, _, value = part.strip().partition(b"=")
        counts[key] = int(value) if value.isdigit() else 0
    return (
        counts.get(b"failures", 0) > 0
        and counts.get(b"errors", 0) == 0
        and (counts.get(b"unexpected successes", 0) == 0)
    )


def _run_bounded(command: list[str], cwd: Path, environment: dict[str, str]):
    """Run ``command`` and return (returncode, the last ``_MAX_CAPTURE`` bytes)."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tail = bytearray()
    truncated = False
    try:
        while chunk := process.stdout.read(64 * 1024):
            tail += chunk
            if len(tail) > _MAX_CAPTURE:
                del tail[: len(tail) - _MAX_CAPTURE]
                truncated = True
        returncode = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
    if truncated:
        # Drop the partial first line so no cut-off fragment (of a URL, say) is kept.
        newline = tail.find(b"\n")
        tail = tail[newline + 1 :] if newline >= 0 else bytearray()
    return returncode, bytes(tail)


def _run_unittest(arguments: argparse.Namespace) -> int:
    worktree = Path(arguments.worktree).resolve()
    overlay = Path(arguments.overlay).resolve() if arguments.overlay else None
    temporary = Path(tempfile.mkdtemp(prefix="paw-seed-check-"))
    cluster: _Cluster | None = None
    code = 1
    try:
        tree = temporary / "tree"
        _copy_tree(worktree, tree)
        if overlay is not None:
            _apply_overlay(overlay, tree)
        environment = dict(os.environ)
        environment.pop("PAW_TEST_DATABASE_URL", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if arguments.postgres_image:
            cluster = _start_cluster(arguments.postgres_image)
            environment["PAW_TEST_DATABASE_URL"] = cluster.test_url
        command = [sys.executable, "-m", "unittest", "-v", *arguments.tests]
        returncode, output = _run_bounded(
            command, tree / arguments.workdir, environment
        )
        if cluster is not None:
            output = output.replace(cluster.test_url.encode(), b"<database-url>")
        _echo(output[-_MAX_ECHO:])
        ran = [int(value) for value in _RAN.findall(output)]
        summaries = _SUMMARY.findall(output)
        skipped = sum(int(value) for value in _SKIPPED.findall(output))
        passed = (
            returncode == 0
            and bool(ran)
            and ran[-1] > 0
            and bool(summaries)
            and summaries[-1] == b"OK"
        )
        refused_skips = bool(skipped) and not arguments.allow_skips
        if refused_skips:
            print(f"seed_check: {skipped} test(s) skipped; skips are not allowed")
            passed = False
        if arguments.expect == "fail":
            # A skip is refused on its own: skipping the test against the known bug
            # must not count as "the test detects the bug".
            # Only assertion failures count: an ERROR (import, setup, a crash) is not
            # the test detecting the bug.
            verdict = (
                not passed
                and not refused_skips
                and bool(ran)
                and ran[-1] > 0
                and _only_assertion_failures(output)
            )
        else:
            verdict = passed
        print(
            f"seed_check: expect={arguments.expect} verdict={'pass' if verdict else 'fail'}"
        )
        code = 0 if verdict else 1
    finally:
        # A cluster that could not be removed would outlive the check and could
        # leak into later runs, so a failed teardown fails the check.
        if cluster is not None and not _stop_cluster(cluster):
            code = 1
        shutil.rmtree(temporary, ignore_errors=True)
    return code


def _git(worktree: Path, *arguments: str) -> list[str]:
    # GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE from a caller (a Git hook, for one)
    # would make Git compare another repository or index than the worktree's.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # refs/replace/ in the candidate's repository must not swap the base commit.
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(worktree), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=True,
    )
    return [line for line in completed.stdout.decode().split("\0") if line]


# Evaluator caches that are never part of a candidate's change.
_UNTRACKED_CACHES = frozenset(
    {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
)


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data
    ).hexdigest()  # Git blob id (SHA-1 object format)


def _worktree_blobs(worktree: Path) -> dict[str, tuple[str, str]]:
    """Git (mode, blob id) of the files in the worktree, hashed here from the raw bytes.

    Git's own view (index flags such as ``assume-unchanged`` / ``skip-worktree``,
    ignore rules, attributes and filters) is under the candidate's control, so it
    is not used to decide what changed.
    """
    blobs = {}
    for directory, subdirectories, files in os.walk(worktree):
        here = Path(directory)
        relative = here.relative_to(worktree)
        linked = [name for name in subdirectories if (here / name).is_symlink()]
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in linked
            and name not in _UNTRACKED_CACHES
            and not (relative == Path(".") and name == ".git")
        ]
        for name in (*files, *linked):
            if relative == Path(".") and name == ".git":
                continue  # a linked worktree has a .git file
            path = here / name
            key = (relative / name).as_posix()
            if path.is_symlink():
                blobs[key] = ("120000", _blob_id(os.fsencode(os.readlink(path))))
            elif path.is_file():
                # Git records the executable bit from the owner bit only.
                mode = "100755" if path.stat().st_mode & 0o100 else "100644"
                blobs[key] = (mode, _blob_id(path.read_bytes()))
    return blobs


def _base_blobs(worktree: Path, base: str) -> dict[str, tuple[str, str]]:
    blobs = {}
    for entry in _git(worktree, "ls-tree", "-r", "-z", "--full-tree", base):
        meta, _, path = entry.partition("\t")
        mode, kind, object_id = meta.split()
        if kind == "blob":
            blobs[path] = (mode, object_id)
    return blobs


def _allowed(path: str, allowances: list[str]) -> bool:
    """An allowance is an exact path, or a directory when it ends with ``/``."""
    return any(
        path.startswith(allowance) if allowance.endswith("/") else path == allowance
        for allowance in allowances
    )


def _run_forbidden_changes(arguments: argparse.Namespace) -> int:
    worktree = Path(arguments.worktree).resolve()
    try:
        base = _base_blobs(worktree, arguments.base)
    except subprocess.CalledProcessError:
        print("seed_check: could not read the base commit")
        return 1
    current = _worktree_blobs(worktree)
    changed = {
        path
        for path in base.keys() | current.keys()
        if base.get(path) != current.get(path)
    }
    outside = sorted(path for path in changed if not _allowed(path, arguments.allow))
    for path in outside:
        print(f"seed_check: change outside the allowed paths: {path}")
    print(f"seed_check: forbidden-changes verdict={'fail' if outside else 'pass'}")
    return 1 if outside else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    unit = sub.add_parser("unittest")
    unit.add_argument("--worktree", default=".")
    unit.add_argument("--overlay")
    unit.add_argument("--workdir", default=".")
    unit.add_argument("--postgres-image")
    unit.add_argument("--allow-skips", action="store_true")
    unit.add_argument("--expect", choices=("pass", "fail"), default="pass")
    unit.add_argument("tests", nargs="+")
    forbidden = sub.add_parser("forbidden-changes")
    forbidden.add_argument("--worktree", default=".")
    forbidden.add_argument("--base", required=True)
    forbidden.add_argument("--allow", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, _on_term)
    try:
        if arguments.mode == "unittest":
            return _run_unittest(arguments)
        return _run_forbidden_changes(arguments)
    except _Terminated:
        return 124


if __name__ == "__main__":
    sys.exit(main())
