"""Keep the three lists of backend/CI dependencies identical.

CI runs `run_ci.py` inside the pre-commit hook environment, which contains
only what `additional_dependencies` in `.pre-commit-config.yaml` installs. The
same pins are repeated in `.github/requirements-ci.txt` (standalone setup) and
`apps/backend/pyproject.toml` (the application). A drift between them would
surface as an import error in CI, or worse, as tests run against versions that
differ from the ones the application declares.
"""

import ast
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "apps/backend"
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([^\]]+)\])?==([A-Za-z0-9.+!_-]+)")
# The driver installed by requirements-ci.txt; it is not a hook dependency.
DRIVER = "pre-commit"


def parse_pin(spec):
    """Return (normalized name, extras, version) or fail for a non-exact pin."""
    match = PIN.fullmatch(spec.strip())
    if match is None:
        raise AssertionError(f"not an exact `name==version` pin: {spec!r}")
    name, extras, version = match.groups()
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    return normalized, frozenset((extras or "").replace(" ", "").split(",")) - {""}, version


def hook_dependencies():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    (hook,) = [hook for hook in hooks if hook["id"] == "repository-checks"]
    return [parse_pin(spec) for spec in hook["additional_dependencies"]]


def requirements_ci():
    lines = (ROOT / ".github/requirements-ci.txt").read_text().splitlines()
    return [parse_pin(line) for line in lines if line.strip()]


# Only the backend's own code is scanned. A developer's `.venv` (the README
# tells them to create one in apps/backend), caches and other hidden or
# generated directories contain third-party code that is not ours.
SCANNED_DIRECTORIES = ("paw_backend", "tests", "migrations")
FIRST_PARTY = {"paw_backend", "tests", "migrations"}


def imported_third_party_packages(backend):
    """Normalized names of the third-party packages the backend code imports."""
    imported = set()
    for directory in SCANNED_DIRECTORIES:
        for folder, subfolders, files in os.walk(backend / directory):
            subfolders[:] = [
                name
                for name in subfolders
                if not name.startswith(".") and name != "__pycache__"
            ]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = Path(folder, name)
                for node in ast.walk(ast.parse(path.read_text(), str(path))):
                    if isinstance(node, ast.Import):
                        imported.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names) - FIRST_PARTY
    return {re.sub(r"[-_.]+", "-", name) for name in third_party}


def backend_pins():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    specs = project["project"]["dependencies"] + project["dependency-groups"]["dev"]
    return [parse_pin(spec) for spec in specs]


class DependencyPinsTest(unittest.TestCase):
    def test_requirements_ci_matches_the_hook_environment(self):
        standalone = [pin for pin in requirements_ci() if pin[0] != DRIVER]
        self.assertEqual(sorted(standalone), sorted(hook_dependencies()))

    def test_requirements_ci_installs_the_hook_driver(self):
        self.assertIn(DRIVER, [pin[0] for pin in requirements_ci()])

    def test_hook_environment_contains_the_backend_dependencies(self):
        missing = set(backend_pins()) - set(hook_dependencies())
        self.assertEqual(missing, set(), "pinned in pyproject.toml but not in the hook")

    def test_each_dependency_is_pinned_once(self):
        for pins in (hook_dependencies(), requirements_ci(), backend_pins()):
            names = [name for name, _, _ in pins]
            self.assertEqual(len(names), len(set(names)), names)

    def test_backend_imports_only_declared_third_party_packages(self):
        declared = {name for name, _, _ in backend_pins()}
        undeclared = imported_third_party_packages(BACKEND) - declared
        self.assertEqual(undeclared, set(), "imported but not pinned in pyproject.toml")


class ImportScanTest(unittest.TestCase):
    def write(self, root, relative, source):
        path = Path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def test_scans_only_the_backend_code_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, "paw_backend/app.py", "import fastapi\nimport os\n")
            self.write(directory, "tests/test_x.py", "from httpx import Client\n")
            self.write(directory, "migrations/env.py", "from alembic import context\n")
            # None of these belong to the backend, however they got there.
            self.write(directory, ".venv/lib/site.py", "import numpy\n")
            self.write(directory, "paw_backend/.hidden/x.py", "import pandas\n")
            self.write(directory, "paw_backend/__pycache__/y.py", "import scipy\n")
            self.write(directory, "build/z.py", "import requests\n")
            found = imported_third_party_packages(Path(directory))
        self.assertEqual(found, {"fastapi", "httpx", "alembic"})

    def test_first_party_and_relative_imports_are_not_third_party(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(
                directory,
                "tests/test_x.py",
                "from paw_backend.app import create_app\nfrom .support import x\n"
                "from tests import support\nimport pydantic_settings\n",
            )
            found = imported_third_party_packages(Path(directory))
        self.assertEqual(found, {"pydantic-settings"})


if __name__ == "__main__":
    unittest.main()
