"""Seed benchmark dataset (PAW-016): manifest checks, private build and golden verification.

The public part of a dataset version (task manifests and the dataset index) lives in
``benchmarks/seed-tasks/<dataset>`` (``paw-seed-v1``, ``paw-seed-v2``, ...).  Everything
a candidate must not see (hidden tests, bug patches, golden patches and the per-task Git
repositories) lives in an evaluator-private directory outside this repository, by
default ``/data/datasets/<dataset>`` (Decision 0041).  This module holds none of that
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

A later version may list tasks of an earlier version unchanged (Decision 0041 2: a
version used by a comparison run is never edited).  Such a manifest entry names the
earlier ``dataset``; its task file, hidden checks and private material stay where that
version keeps them (``benchmarks/seed-tasks/<earlier>/tasks`` and
``<private base>/<earlier>``), and the entry must equal the earlier version's entry.

Commands::

    python -m benchmarks.seed_dataset [--dataset NAME] check
    python -m benchmarks.seed_dataset [--dataset NAME] build --source-repo . [--private-root DIR]
    python -m benchmarks.seed_dataset [--dataset NAME] verify [--private-root DIR] \\
        [--private-base DIR] --work-dir DIR [--postgres-image IMAGE] [--task TASK_ID ...]
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

SEED_TASKS_DIRECTORY = Path(__file__).parent / "seed-tasks"
DEFAULT_DATASET = "paw-seed-v1"
DATASET_DIRECTORY = SEED_TASKS_DIRECTORY / DEFAULT_DATASET
MANIFEST_PATH = DATASET_DIRECTORY / "manifest.json"
DEFAULT_PRIVATE_BASE = Path("/data/datasets")
DEFAULT_PRIVATE_ROOT = DEFAULT_PRIVATE_BASE / DEFAULT_DATASET
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
LOCATOR_SCHEME = "paw-dataset://"
LOCATOR_PREFIX = LOCATOR_SCHEME + DEFAULT_DATASET + "/"
_DATASET_NAME = re.compile(r"^paw-seed-v([1-9][0-9]*)$")
_TASK_SUFFIX = re.compile(r"^(hist|spec|bug)-[0-9]{2}-[a-z0-9-]+$")
_REFERENCE_SUFFIX = re.compile(r"^[0-9a-f]{12}$")
# Obvious credential shapes; a match anywhere in the public dataset is an error.
_SECRET_PATTERNS = (
    re.compile(r"postgres(ql)?(\+\w+)?://[^\s\"'@/]+:[^\s\"'@/]+@"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
# Author / committer date of the parentless starting commits; a later version records
# its own in the manifest (``snapshot_date``) so the commit ids stay reproducible.
DEFAULT_SNAPSHOT_DATE = "2026-09-28T00:00:00+00:00"


def _snapshot_env(dataset: str, date: str) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": dataset,
        "GIT_AUTHOR_EMAIL": f"{dataset}@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": dataset,
        "GIT_COMMITTER_EMAIL": f"{dataset}@example.invalid",
        "GIT_COMMITTER_DATE": date,
    }


def dataset_version(name: Any) -> int | None:
    """``N`` of a dataset named ``paw-seed-vN``, ``None`` for any other value."""
    if not isinstance(name, str):
        return None
    match = _DATASET_NAME.fullmatch(name)
    return int(match.group(1)) if match else None


def manifest_path(dataset: str) -> Path:
    if dataset_version(dataset) is None:
        raise SeedDatasetError("dataset must be named paw-seed-v<N>")
    return SEED_TASKS_DIRECTORY / dataset / "manifest.json"


def _valid_task_id(task_id: Any, dataset: str) -> bool:
    prefix = dataset + "-"
    return (
        isinstance(task_id, str)
        and task_id.startswith(prefix)
        and _TASK_SUFFIX.fullmatch(task_id[len(prefix) :]) is not None
    )


def _valid_reference(reference: Any, dataset: str) -> bool:
    prefix = f"seed-v{dataset_version(dataset)}-"
    return (
        isinstance(reference, str)
        and reference.startswith(prefix)
        and _REFERENCE_SUFFIX.fullmatch(reference[len(prefix) :]) is not None
    )


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
    # The version that owns the task (its file, hidden checks and private material).
    dataset: str = DEFAULT_DATASET

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
        dataset = item.get("dataset", manifest["dataset"])
        task_path = path.parent.parent / dataset / item["file"]
        entries.append(
            TaskEntry(
                task_id=item["task_id"],
                path=task_path,
                kind=item["kind"],
                difficulty=item["difficulty"],
                categories=tuple(item["categories"]),
                source=item["source"],
                document=_read_json(task_path),
                dataset=dataset,
            )
        )
    return manifest, entries


def _earlier_entries(
    directory: Path, dataset: str, cache: dict[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Task entries (by id) of the earlier version ``dataset`` next to ``directory``."""
    if dataset not in cache:
        try:
            earlier = _read_json(directory.parent / dataset / "manifest.json")
            cache[dataset] = {
                item["task_id"]: item
                for item in earlier["tasks"]
                if isinstance(item, dict) and "task_id" in item
            }
        except (OSError, ValueError, KeyError, TypeError):
            cache[dataset] = None
    return cache[dataset]


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
    own = manifest["dataset"]
    own_version = dataset_version(own)
    if own_version is None:
        return ["$.dataset: must be named paw-seed-v<N>"]
    schema = load_schema()
    seen_ids: set[str] = set()
    seen_references: set[str] = set()
    listed_files: set[str] = set()
    earlier_cache: dict[str, dict[str, Any] | None] = {}
    for index, item in enumerate(manifest["tasks"]):
        where = f"$.tasks[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected object")
            continue
        dataset = item.get("dataset", own)
        version = dataset_version(dataset)
        if dataset != own:
            # A task of an earlier version, listed unchanged (Decision 0041 2).
            if version is None or version >= own_version:
                errors.append(f"{where}.dataset: must name an earlier version")
                continue
            earlier = _earlier_entries(directory, dataset, earlier_cache)
            if earlier is None:
                errors.append(f"{where}.dataset: the earlier version cannot be read")
                continue
            listed = {key: value for key, value in item.items() if key != "dataset"}
            if earlier.get(item.get("task_id")) != listed:
                errors.append(f"{where}: differs from the entry of the earlier version")
                continue
        task_id = item.get("task_id")
        if not _valid_task_id(task_id, dataset):
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
        if dataset == own:
            listed_files.add(file_name)
        owner = directory if dataset == own else directory.parent / dataset
        task_path = owner / file_name
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
        locator_prefix = f"{LOCATOR_SCHEME}{dataset}/"
        if repository.get("locator") != locator_prefix + task_id:
            errors.append(f"{file_name}: locator must be {locator_prefix}<task_id>")
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
            if not _valid_reference(reference, dataset):
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
    repository: Path,
    base_commit: str,
    patch: Path,
    task_id: str,
    dataset: str = DEFAULT_DATASET,
    date: str = DEFAULT_SNAPSHOT_DATE,
) -> str:
    """Create the parentless starting commit of an injected-bug task."""
    with tempfile.TemporaryDirectory(prefix="paw-seed-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repository, "read-tree", base_commit, env=env)
        _git(
            repository, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env
        )
        tree = _git(repository, "write-tree", env=env).strip()
    message = f"{dataset} starting state of {task_id}\n".encode()
    return _git(
        repository,
        "commit-tree",
        tree,
        env=_snapshot_env(dataset, date),
        input_bytes=message,
    ).strip()


def build(
    source_repo: Path,
    private_root: Path,
    entries: list[TaskEntry],
    base_commit: str,
    dataset: str = DEFAULT_DATASET,
    snapshot_date: str = DEFAULT_SNAPSHOT_DATE,
) -> dict[str, str]:
    """(Re)build the per-task repositories and the historical golden patches.

    Only tasks owned by ``dataset`` are built; tasks listed from an earlier version
    keep the repositories that version built.
    """
    built = {}
    repos = private_root / "repos"
    repos.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in entries:
        if entry.dataset != dataset:
            continue
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
                target,
                base_commit,
                task_dir / "bug.patch",
                entry.task_id,
                dataset,
                snapshot_date,
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
    private_root: Path | dict[str, Path],
    work_dir: Path,
    entries: list[TaskEntry],
    postgres_image: str | None,
    timeout: float = 3600.0,
) -> list[dict[str, Any]]:
    """Run every hidden check on the starting and on the golden state.

    A task passes when every hidden check has its expected status on both states
    (acceptance: ``failed`` then ``passed``; regression: ``passed`` twice) and every
    visible check passes on the golden state.  ``private_root`` is one directory, or
    the private directory of each dataset version that owns a listed task.
    """
    from benchmarks.worktree_runner import WorktreeRunner

    roots = (
        private_root
        if isinstance(private_root, dict)
        else {entry.dataset: private_root for entry in entries}
    )
    all_definitions: dict[str, dict[str, dict[str, Any]]] = {}
    reports = []
    for entry in entries:
        private_root = roots[entry.dataset]
        if entry.dataset not in all_definitions:
            all_definitions[entry.dataset] = load_hidden_definitions(private_root)
        definitions = all_definitions[entry.dataset]
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
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help="dataset version under benchmarks/seed-tasks (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the public dataset index and tasks")
    build_parser = sub.add_parser("build", help="build the private task repositories")
    build_parser.add_argument("--source-repo", type=Path, required=True)
    build_parser.add_argument(
        "--private-root",
        type=Path,
        help="private directory of the dataset (default: <private base>/<dataset>)",
    )
    build_parser.add_argument("--private-base", type=Path, default=DEFAULT_PRIVATE_BASE)
    build_parser.add_argument("--task", action="append", default=[])
    verify_parser = sub.add_parser("verify", help="verify golden behaviour")
    verify_parser.add_argument(
        "--private-root",
        type=Path,
        help="private directory of the dataset (default: <private base>/<dataset>)",
    )
    verify_parser.add_argument(
        "--private-base",
        type=Path,
        default=DEFAULT_PRIVATE_BASE,
        help="parent of the private directories of earlier versions",
    )
    verify_parser.add_argument("--work-dir", type=Path, required=True)
    verify_parser.add_argument("--postgres-image")
    verify_parser.add_argument("--task", action="append", default=[])
    verify_parser.add_argument("--report", type=Path)
    arguments = parser.parse_args(argv)

    try:
        manifest, entries = load_manifest(manifest_path(arguments.dataset))
    except (OSError, ValueError, SeedDatasetError) as error:
        print(f"dataset is invalid: {error}", file=sys.stderr)
        return 1
    if arguments.command == "check":
        print(json.dumps(dataset_summary(entries), indent=2, ensure_ascii=False))
        return 0
    if arguments.task:
        wanted = set(arguments.task)
        entries = [entry for entry in entries if entry.task_id in wanted]
    own_root = arguments.private_root or arguments.private_base / arguments.dataset
    if arguments.command == "build":
        built = build(
            arguments.source_repo.resolve(),
            own_root,
            entries,
            manifest["base_commit"],
            arguments.dataset,
            manifest.get("snapshot_date", DEFAULT_SNAPSHOT_DATE),
        )
        for task_id, commit in built.items():
            print(f"{task_id}: {commit}")
        return 0
    roots = {
        entry.dataset: (
            own_root
            if entry.dataset == arguments.dataset
            else arguments.private_base / entry.dataset
        )
        for entry in entries
    }
    reports = verify(
        roots,
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
