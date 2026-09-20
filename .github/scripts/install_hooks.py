"""Install pre-commit without replacing an existing Git hook setup."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
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
    return subprocess.run(
        [sys.executable, "-m", "pre_commit", "install", "--install-hooks",
         "--hook-type", "pre-commit"], cwd=root,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
