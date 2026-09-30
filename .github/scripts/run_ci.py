"""Run the same repository checks in Git hooks and GitHub Actions."""

from pathlib import Path
import os
import shutil
import subprocess
import sys

# The Web App (apps/web, PAW-060): installed from its lockfile, then lint, format,
# type check, unit tests and a production build (`npm run ci`, see its package.json).
WEB_DIRECTORY = "apps/web"
# In GitHub Actions (or when this is set to 1) the web checks are required: a
# missing Node.js fails the run instead of skipping the checks.
REQUIRE_WEB_ENV = "PAW_REQUIRE_WEB_CHECKS"


def web_checks_required(environ):
    return environ.get("GITHUB_ACTIONS") == "true" or environ.get(REQUIRE_WEB_ENV) == "1"


def web_commands(root, environ=os.environ, which=shutil.which):
    """The web commands, `[]` to skip them locally, or `None` if they cannot run.

    A developer without Node.js can still run the other checks (with a warning);
    CI cannot skip them.
    """
    if not (root / WEB_DIRECTORY / "package.json").is_file():
        return []
    npm = which("npm")
    if npm is None:
        if web_checks_required(environ):
            print(
                "Web checks: npm was not found, and the checks are required here "
                f"(GITHUB_ACTIONS or {REQUIRE_WEB_ENV}=1). Install Node.js "
                f"(the version in {WEB_DIRECTORY}/.node-version).",
                file=sys.stderr,
            )
            return None
        print(
            f"WARNING: npm was not found; the web checks ({WEB_DIRECTORY}) are SKIPPED. "
            f"Install Node.js (the version in {WEB_DIRECTORY}/.node-version) to run "
            "them; CI always runs them.",
            file=sys.stderr,
        )
        return []
    return [
        [npm, "ci", "--no-audit", "--no-fund"],
        [npm, "run", "ci"],
    ]


def main():
    root = Path(__file__).resolve().parents[2]
    commands = (
        [sys.executable, "-m", "ruff", "format", "--check", "benchmarks"],
        [sys.executable, "-m", "ruff", "check", "benchmarks"],
        [sys.executable, "-m", "ruff", "format", "--check", "apps/backend"],
        [sys.executable, "-m", "ruff", "check", "apps/backend"],
        [sys.executable, "-m", "unittest", "discover", "-s", ".github/scripts",
         "-p", "test_*.py", "-v"],
        [sys.executable, "-m", "unittest", "discover", "-s", "benchmarks/tests",
         "-p", "test_*.py", "-v"],
        # `-t` puts apps/backend on sys.path so the tests import `paw_backend`
        # without installing it. Set PAW_TEST_DATABASE_URL to also run the
        # PostgreSQL integration tests; they are skipped otherwise.
        [sys.executable, "-m", "unittest", "discover", "-s", "apps/backend/tests",
         "-t", "apps/backend", "-p", "test_*.py", "-v"],
        [sys.executable, ".github/scripts/check_repository.py"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            return result.returncode
    web = web_commands(root)
    if web is None:
        return 1
    for command in web:
        result = subprocess.run(command, cwd=root / WEB_DIRECTORY)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
