"""Install pre-commit without replacing an existing Git hook setup."""

from pathlib import Path
import subprocess
import sys


def check_existing_hooks(root):
    hooks_path = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=root, capture_output=True,
    )
    if hooks_path.returncode == 0:
        print("Refusing to replace the existing core.hooksPath setting.", file=sys.stderr)
        return 1
    if hooks_path.returncode != 1:
        print("Could not inspect Git hook configuration.", file=sys.stderr)
        return hooks_path.returncode
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"], cwd=root, check=True,
        capture_output=True, text=True,
    )
    hooks = root / result.stdout.strip()
    for name in ("pre-commit", "pre-commit.legacy"):
        path = hooks / name
        if path.exists() or path.is_symlink():
            print(f"Refusing to overwrite existing Git hook: {path}", file=sys.stderr)
            return 1
    return 0


def install(root):
    # Prepare dependencies before installing a hook so failures remain retryable.
    for command in (("install-hooks",), ("install", "--hook-type", "pre-commit")):
        conflict = check_existing_hooks(root)
        if conflict:
            return conflict
        result = subprocess.run([sys.executable, "-m", "pre_commit", *command], cwd=root)
        if result.returncode:
            return result.returncode
    return 0


def main():
    return install(Path(__file__).resolve().parents[2])


if __name__ == "__main__":
    sys.exit(main())
