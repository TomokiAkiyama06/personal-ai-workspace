"""Run the same repository checks in Git hooks and GitHub Actions."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    commands = (
        [sys.executable, "-m", "unittest", "discover", "-s", ".github/scripts",
         "-p", "test_*.py", "-v"],
        [sys.executable, ".github/scripts/check_repository.py"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
