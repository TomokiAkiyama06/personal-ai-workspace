"""Seed benchmark dataset (PAW-016): manifest checks, private build and golden verification.

The public part of the dataset (task manifests and the dataset index) lives in
``benchmarks/seed-tasks/paw-seed-v1``.  Everything a candidate must not see (hidden
tests, bug patches, golden patches and the per-task Git repositories) lives in an
evaluator-private directory outside this repository, by default
``/data/datasets/paw-seed-v1`` (Decision 0041).  This module holds none of that
material; it only knows the layout:

``<private>/hidden-checks.json``
    Opaque ``reference_id`` -> check definition (``seed_check`` mode and arguments,
    and the expected verdict on the starting and on the golden state).
``<private>/overlays/<reference_id>/``
    Files the hidden check lays over a copy of the candidate tree.
``<private>/tasks/<task_id>/golden.patch``
    Known-good change from the starting commit (historical tasks: generated from the
    merged PR by ``build``; spec / injected-bug tasks: authored).
``<private>/tasks/<task_id>/bug.patch``
    Injected-bug tasks only: applied to ``base_commit`` to make the starting state.
``<private>/repos/<task_id>/``
    Built by ``build``: a repository that holds only the starting commit and its
    ancestors (historical / spec) or a parentless snapshot of the bugged tree
    (injected bug), so the candidate cannot read the fix from Git history.

Commands::

    python -m benchmarks.seed_dataset check
    python -m benchmarks.seed_dataset build --source-repo . --private-root DIR
    python -m benchmarks.seed_dataset verify --private-root DIR --work-dir DIR \\
        [--postgres-image IMAGE] [--task TASK_ID ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.json_input import decode_json
from benchmarks.validate_task import load_schema, validate_document

DATASET_DIRECTORY = Path(__file__).parent / "seed-tasks" / "paw-seed-v1"
MANIFEST_PATH = DATASET_DIRECTORY / "manifest.json"
DEFAULT_PRIVATE_ROOT = Path("/data/datasets/paw-seed-v1")
SEED_CHECK = Path(__file__).resolve().parent / "seed_check.py"

KINDS = ("historical", "spec", "injected_bug")
DIFFICULTIES = ("easy", "medium", "hard")
CATEGORIES = (
    "feature",
    "bug_fix",
    "security",
    "multi_file",
    "repo_exploration",
    "test_fix",
    "schema_or_validation",
    "concurrency_or_process",
    "database_migration",
    "api_contract",
    "metrics",
)
LOCATOR_PREFIX = "paw-dataset://paw-seed-v1/"
_REFERENCE = re.compile(r"^seed-v1-[0-9a-f]{12}$")
_TASK_ID = re.compile(r"^paw-seed-v1-(hist|spec|bug)-[0-9]{2}-[a-z0-9-]+$")
# Obvious credential shapes; a match anywhere in the public dataset is an error.
_SECRET_PATTERNS = (
    re.compile(r"postgres(ql)?(\+\w+)?://[^\s\"'@/]+:[^\s\"'@/]+@"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_SNAPSHOT_ENV = {
    "GIT_AUTHOR_NAME": "paw-seed-v1",
    "GIT_AUTHOR_EMAIL": "paw-seed-v1@example.invalid",
    "GIT_AUTHOR_DATE": "2026-09-28T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "paw-seed-v1",
    "GIT_COMMITTER_EMAIL": "paw-seed-v1@example.invalid",
    "GIT_COMMITTER_DATE": "2026-09-28T00:00:00+00:00",
}


class SeedDatasetError(RuntimeError):
    """The dataset or its private material is inconsistent."""


@dataclass(frozen=True)
class TaskEntry:
    task_id: str
    path: Path
    kind: str
    difficulty: str
    categories: tuple[str, ...]
    source: dict[str, Any]
    document: dict[str, Any]

    @property
    def starting_commit(self) -> str:
        return self.document["repository"]["starting_commit"]


def _read_json(path: Path) -> Any:
    return decode_json(path.read_text(encoding="utf-8"))


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[dict[str, Any], list[TaskEntry]]:
    """Load and cross-check the public dataset index and every task manifest."""
    manifest = _read_json(path)
    errors = check_manifest(manifest, path.parent)
    if errors:
        raise SeedDatasetError("; ".join(errors))
    entries = []
    for item in manifest["tasks"]:
        task_path = path.parent / item["file"]
        entries.append(
            TaskEntry(
                task_id=item["task_id"],
                path=task_path,
                kind=item["kind"],
                difficulty=item["difficulty"],
                categories=tuple(item["categories"]),
                source=item["source"],
                document=_read_json(task_path),
            )
        )
    return manifest, entries


def check_manifest(manifest: Any, directory: Path) -> list[str]:
    """Return value-free errors of the dataset index and the task files it lists."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["$: expected object"]
    for key in ("dataset", "base_commit", "tasks"):
        if key not in manifest:
            errors.append(f"$: required property is missing: {key}")
    if errors:
        return errors
    schema = load_schema()
    seen_ids: set[str] = set()
    seen_references: set[str] = set()
    listed_files: set[str] = set()
    for index, item in enumerate(manifest["tasks"]):
        where = f"$.tasks[{index}]"
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            errors.append(f"{where}.task_id: does not follow the naming rule")
            continue
        if task_id in seen_ids:
            errors.append(f"{where}.task_id: duplicate task id")
        seen_ids.add(task_id)
        if item.get("kind") not in KINDS:
            errors.append(f"{where}.kind: value is not an allowed enum member")
        if item.get("difficulty") not in DIFFICULTIES:
            errors.append(f"{where}.difficulty: value is not an allowed enum member")
        categories = item.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or any(category not in CATEGORIES for category in categories)
            or len(set(categories)) != len(categories)
        ):
            errors.append(f"{where}.categories: must be unique known categories")
        if not isinstance(item.get("source"), dict):
            errors.append(f"{where}.source: expected object")
        file_name = item.get("file")
        if not isinstance(file_name, str) or file_name != f"tasks/{task_id}.json":
            errors.append(f"{where}.file: must be tasks/<task_id>.json")
            continue
        listed_files.add(file_name)
        task_path = directory / file_name
        try:
            document = _read_json(task_path)
        except (OSError, ValueError):
            errors.append(f"{where}.file: cannot be read as strict JSON")
            continue
        for error in validate_document(document, schema):
            errors.append(f"{file_name}:{error}")
        if not isinstance(document, dict):
            continue
        if document.get("task_id") != task_id:
            errors.append(f"{file_name}: task_id differs from the dataset index")
        if document.get("kind") != item.get("kind"):
            errors.append(f"{file_name}: kind differs from the dataset index")
        repository = document.get("repository", {})
        if repository.get("locator") != LOCATOR_PREFIX + task_id:
            errors.append(f"{file_name}: locator must be {LOCATOR_PREFIX}<task_id>")
        if "known_good_commit" in repository:
            # The known-good state is private (golden patch); a public commit id
            # would point the candidate at the fix.
            errors.append(f"{file_name}: known_good_commit must stay private")
        hidden = document.get("hidden_checks", [])
        if not hidden:
            errors.append(f"{file_name}: at least one hidden check is required")
        ids = [check.get("id") for check in document.get("visible_checks", [])]
        ids += [check.get("id") for check in hidden]
        if len(ids) != len(set(ids)):
            errors.append(f"{file_name}: check ids must be unique")
        for check in hidden:
            reference = check.get("reference_id")
            if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
                errors.append(f"{file_name}: hidden reference_id must be opaque")
            elif reference in seen_references:
                errors.append(f"{file_name}: hidden reference_id is reused")
            else:
                seen_references.add(reference)
        text = task_path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            errors.append(f"{file_name}: looks like it contains a credential")
    on_disk = {f"tasks/{path.name}" for path in (directory / "tasks").glob("*.json")}
    for extra in sorted(on_disk - listed_files):
        errors.append(f"{extra}: task file is not listed in the dataset index")
    return errors


def dataset_summary(entries: list[TaskEntry]) -> dict[str, Any]:
    """Counts by kind, difficulty and category (for the README and the Decision)."""
    summary: dict[str, Any] = {"total": len(entries), "kind": {}, "difficulty": {}}
    categories: dict[str, int] = {}
    for entry in entries:
        summary["kind"][entry.kind] = summary["kind"].get(entry.kind, 0) + 1
        summary["difficulty"][entry.difficulty] = (
            summary["difficulty"].get(entry.difficulty, 0) + 1
        )
        for category in entry.categories:
            categories[category] = categories.get(category, 0) + 1
    summary["category"] = dict(sorted(categories.items()))
    return summary


# --------------------------------------------------------------------- private


def _git(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(env or {})
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repository), *arguments],
        input=input_bytes,
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise SeedDatasetError(
            f"git {arguments[0]} failed: {completed.stderr.decode(errors='replace')[:400]}"
        )
    return completed.stdout.decode()


def load_hidden_definitions(private_root: Path) -> dict[str, dict[str, Any]]:
    document = _read_json(private_root / "hidden-checks.json")
    if document.get("format") != "paw-seed-hidden-v1":
        raise SeedDatasetError("hidden-checks.json has an unknown format")
    return document["checks"]


def hidden_command(
    definition: dict[str, Any], private_root: Path, postgres_image: str | None
) -> tuple[str, ...]:
    """Build the evaluator-side argv of one hidden check (never shown to a candidate)."""
    command = [sys.executable, str(SEED_CHECK), definition["mode"]]
    if definition["mode"] == "unittest":
        if definition.get("overlay"):
            command += ["--overlay", str(private_root / definition["overlay"])]
        command += ["--workdir", definition.get("workdir", ".")]
        if definition.get("postgres"):
            if not postgres_image:
                raise SeedDatasetError(
                    "a PostgreSQL hidden check needs --postgres-image"
                )
            command += ["--postgres-image", postgres_image]
        if definition.get("allow_skips"):
            command.append("--allow-skips")
        command += ["--expect", definition.get("expect", "pass")]
        command += list(definition["tests"])
    elif definition["mode"] == "forbidden-changes":
        command += ["--base", definition["base"]]
        for prefix in definition["allow"]:
            command += ["--allow", prefix]
    else:
        raise SeedDatasetError("unknown hidden check mode")
    return tuple(command)


def build_registry(
    private_root: Path,
    postgres_image: str | None,
    references: set[str] | None = None,
):
    """Build the evaluator's registry (only ``references`` when given)."""
    from benchmarks.test_runner import CheckDefinition, HiddenCheckRegistry

    checks = {}
    for reference_id, definition in load_hidden_definitions(private_root).items():
        if references is not None and reference_id not in references:
            continue
        checks[reference_id] = CheckDefinition(
            id=definition["id"],
            type=definition["type"],
            command=hidden_command(definition, private_root, postgres_image),
        )
    return HiddenCheckRegistry(checks)


def snapshot_commit(
    repository: Path, base_commit: str, patch: Path, task_id: str
) -> str:
    """Create the parentless starting commit of an injected-bug task."""
    with tempfile.TemporaryDirectory(prefix="paw-seed-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repository, "read-tree", base_commit, env=env)
        _git(
            repository, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env
        )
        tree = _git(repository, "write-tree", env=env).strip()
    message = f"paw-seed-v1 starting state of {task_id}\n".encode()
    return _git(
        repository, "commit-tree", tree, env=_SNAPSHOT_ENV, input_bytes=message
    ).strip()


def build(
    source_repo: Path, private_root: Path, entries: list[TaskEntry], base_commit: str
) -> dict[str, str]:
    """(Re)build the per-task repositories and the historical golden patches."""
    built = {}
    repos = private_root / "repos"
    repos.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in entries:
        task_dir = private_root / "tasks" / entry.task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = repos / entry.task_id
        if target.exists():
            shutil.rmtree(target)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        if entry.kind == "injected_bug":
            _git(
                target,
                "fetch",
                "-q",
                "--no-tags",
                str(source_repo),
                f"{base_commit}:refs/seed/base",
            )
            commit = snapshot_commit(
                target, base_commit, task_dir / "bug.patch", entry.task_id
            )
            _git(target, "update-ref", "refs/heads/seed", commit)
            _git(target, "update-ref", "-d", "refs/seed/base")
        else:
            commit = entry.starting_commit
            _git(
                target,
                "fetch",
                "-q",
                "--no-tags",
                str(source_repo),
                f"{commit}:refs/heads/seed",
            )
            if entry.kind == "historical":
                known_good = entry.source["merge_commit"]
                patch = _git(source_repo, "diff", "--binary", commit, known_good)
                (task_dir / "golden.patch").write_text(patch, encoding="utf-8")
        _git(target, "reflog", "expire", "--expire=now", "--all")
        _git(target, "gc", "-q", "--prune=now")
        if commit != entry.starting_commit:
            raise SeedDatasetError(
                f"{entry.task_id}: built starting commit {commit} differs from the manifest"
            )
        built[entry.task_id] = commit
    return built


def _run_hidden(
    worktree: Path, log: Path, references: list[str], registry, timeout: float
) -> dict[str, str]:
    from benchmarks.test_runner import TestRunner

    runner = TestRunner(worktree, log)
    results = runner.run_hidden(references, registry, timeout)
    return {result.id: result.status for result in results}


def _run_visible(
    worktree: Path, log: Path, checks: list[dict[str, Any]], timeout: float
) -> dict[str, str]:
    from benchmarks.test_runner import CheckDefinition, TestRunner

    runner = TestRunner(worktree, log)
    definitions = [
        CheckDefinition(id=c["id"], type=c["type"], command=tuple(c["command"]))
        for c in checks
    ]
    return {r.id: r.status for r in runner.run_visible(definitions, timeout)}


def verify(
    private_root: Path,
    work_dir: Path,
    entries: list[TaskEntry],
    postgres_image: str | None,
    timeout: float = 3600.0,
) -> list[dict[str, Any]]:
    """Run every hidden check on the starting and on the golden state.

    A task passes when every hidden check has its expected status on both states
    (acceptance: ``failed`` then ``passed``; regression: ``passed`` twice) and every
    visible check passes on the golden state.
    """
    from benchmarks.worktree_runner import WorktreeRunner

    definitions = load_hidden_definitions(private_root)
    reports = []
    for entry in entries:
        repository = private_root / "repos" / entry.task_id
        runner = WorktreeRunner(
            repository, work_dir / "runs", work_dir / "run-logs", harden_process=False
        )
        references = [c["reference_id"] for c in entry.document["hidden_checks"]]
        # One registry per task: check ids are unique within a task, not across tasks.
        registry = build_registry(private_root, postgres_image, set(references))
        report: dict[str, Any] = {"task_id": entry.task_id, "states": {}}
        for state in ("start", "golden"):
            run = runner.create("golden-verify", entry.starting_commit)
            try:
                if state == "golden":
                    _git(
                        run.path,
                        "apply",
                        "--whitespace=nowarn",
                        str(private_root / "tasks" / entry.task_id / "golden.patch"),
                    )
                log = work_dir / "check-logs" / f"{entry.task_id}-{state}.jsonl"
                hidden = _run_hidden(run.path, log, references, registry, timeout)
                visible = _run_visible(
                    run.path, log, entry.document["visible_checks"], timeout
                )
            finally:
                runner.cleanup(run)
            report["states"][state] = {"hidden": hidden, "visible": visible}
        problems = []
        for check in entry.document["hidden_checks"]:
            expected = definitions[check["reference_id"]]["expected"]
            for state in ("start", "golden"):
                actual = report["states"][state]["hidden"][check["id"]]
                if actual != expected[state]:
                    problems.append(
                        f"{check['id']} on {state}: {actual} (expected {expected[state]})"
                    )
        for check_id, status in report["states"]["golden"]["visible"].items():
            if status != "passed":
                problems.append(f"visible {check_id} on golden: {status}")
        report["ok"] = not problems
        report["problems"] = problems
        reports.append(report)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed benchmark dataset tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the public dataset index and tasks")
    build_parser = sub.add_parser("build", help="build the private task repositories")
    build_parser.add_argument("--source-repo", type=Path, required=True)
    build_parser.add_argument("--private-root", type=Path, default=DEFAULT_PRIVATE_ROOT)
    build_parser.add_argument("--task", action="append", default=[])
    verify_parser = sub.add_parser("verify", help="verify golden behaviour")
    verify_parser.add_argument(
        "--private-root", type=Path, default=DEFAULT_PRIVATE_ROOT
    )
    verify_parser.add_argument("--work-dir", type=Path, required=True)
    verify_parser.add_argument("--postgres-image")
    verify_parser.add_argument("--task", action="append", default=[])
    verify_parser.add_argument("--report", type=Path)
    arguments = parser.parse_args(argv)

    try:
        manifest, entries = load_manifest()
    except SeedDatasetError as error:
        print(f"dataset is invalid: {error}", file=sys.stderr)
        return 1
    if arguments.command == "check":
        print(json.dumps(dataset_summary(entries), indent=2, ensure_ascii=False))
        return 0
    if arguments.task:
        wanted = set(arguments.task)
        entries = [entry for entry in entries if entry.task_id in wanted]
    if arguments.command == "build":
        built = build(
            arguments.source_repo.resolve(),
            arguments.private_root,
            entries,
            manifest["base_commit"],
        )
        for task_id, commit in built.items():
            print(f"{task_id}: {commit}")
        return 0
    reports = verify(
        arguments.private_root,
        arguments.work_dir.resolve(),
        entries,
        arguments.postgres_image,
    )
    for report in reports:
        status = "ok" if report["ok"] else "FAILED"
        print(f"{report['task_id']}: {status}")
        for problem in report["problems"]:
            print(f"  {problem}")
    if arguments.report:
        arguments.report.write_text(
            json.dumps(reports, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if all(report["ok"] for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
