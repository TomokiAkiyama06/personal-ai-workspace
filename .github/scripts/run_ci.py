"""Run the same repository checks in Git hooks and GitHub Actions."""

from pathlib import Path
import subprocess
import sys


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
