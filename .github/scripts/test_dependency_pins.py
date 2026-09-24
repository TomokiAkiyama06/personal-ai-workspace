"""Keep the three lists of backend/CI dependencies identical.

CI runs `run_ci.py` inside the pre-commit hook environment, which contains
only what `additional_dependencies` in `.pre-commit-config.yaml` installs. The
same pins are repeated in `.github/requirements-ci.txt` (standalone setup) and
`apps/backend/pyproject.toml` (the application). A drift between them would
surface as an import error in CI, or worse, as tests run against versions that
differ from the ones the application declares.
"""

import ast
from pathlib import Path
import re
import sys
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
        first_party = {"paw_backend", "tests", "migrations"}
        imported = set()
        for path in BACKEND.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    imported.add(node.module.split(".")[0])
        third_party = imported - set(sys.stdlib_module_names) - first_party
        undeclared = {re.sub(r"[-_.]+", "-", name) for name in third_party} - declared
        self.assertEqual(undeclared, set(), "imported but not pinned in pyproject.toml")


if __name__ == "__main__":
    unittest.main()
