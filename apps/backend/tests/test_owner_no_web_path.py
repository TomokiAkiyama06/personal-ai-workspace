"""The first web visitor must not become the Owner (PAW-021).

The Owner is created only by the server-local command
(``python -m paw_backend.cli owner-setup``). These tests pin that nothing the web
application can reach creates, replaces or recovers an Owner:

* no HTTP route of the application deals with owner setup or recovery;
* the operator side (``paw_backend.identity.operator``: ``setup_owner``,
  ``recover_owner``) is imported by ``paw_backend.cli`` and ``paw_backend.identity``
  only, **transitively**: a route that reaches it through a helper module is
  found too (an import graph of the whole package, checked with ``ast``);
* what the web side gets, ``TokenRedeemer``, can only spend a token.

PAW-022 adds the web flow that calls ``TokenRedeemer.redeem``: its route needs
``require_capability`` or an entry in the public-route list with its rate-limit
note. It must not import the operator side.
"""

import ast
import re
import tempfile
import unittest
from pathlib import Path

from .support import make_client
from .test_authz_routes import build_app, route_guards

BACKEND_DIR = Path(__file__).resolve().parents[1]
API_DIR = BACKEND_DIR / "paw_backend" / "api"
SETUP_WORDS = re.compile(r"owner|setup|recover|register|signup", re.IGNORECASE)
GUESSES = (
    "/api/v1/setup",
    "/api/v1/setup/owner",
    "/api/v1/owner",
    "/api/v1/owners",
    "/api/v1/users",
    "/api/v1/register",
    "/api/v1/signup",
    "/api/v1/recover",
    "/api/v1/bootstrap/first-user",
    "/setup",
    "/owner",
    "/register",
)

OPERATOR_MODULE = "paw_backend.identity.operator"
# What only the operator side may name (a string constant counts: getattr).
OPERATOR_NAMES = frozenset(
    {"setup_owner", "recover_owner", "OwnerOperator", "OperatorIdentity"}
)
ALLOWED_PREFIXES = ("paw_backend.cli", "paw_backend.identity")


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def parse_package(root: Path) -> dict[str, tuple[ast.Module, bool]]:
    """Every module under ``root/paw_backend``: ``{name: (tree, is_package)}``."""
    modules = {}
    for path in sorted((root / "paw_backend").rglob("*.py")):
        modules[module_name(path, root)] = (
            ast.parse(path.read_text()),
            path.name == "__init__.py",
        )
    return modules


def with_parents(module: str, known: set[str]) -> set[str]:
    """``module`` and its parent packages (importing ``a.b.c`` runs ``a.b``)."""
    parts = module.split(".")
    found = {".".join(parts[: i + 1]) for i in range(len(parts))}
    return found & known


def imports_of(
    tree: ast.Module, module: str, is_package: bool, known: set[str]
) -> set[str]:
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= with_parents(alias.name, known)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            found |= with_parents(base, known)
            for alias in node.names:
                found |= with_parents(f"{base}.{alias.name}", known)
    found.discard(module)
    return found


def names_used(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rpartition(".")[2])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def import_graph(root: Path) -> dict[str, set[str]]:
    modules = parse_package(root)
    known = set(modules)
    return {
        name: imports_of(tree, name, is_package, known)
        for name, (tree, is_package) in modules.items()
    }


def import_closure(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for imported in graph.get(current, ()):
            if imported not in seen:
                seen.add(imported)
                pending.append(imported)
    return seen


def allowed(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in ALLOWED_PREFIXES
    )


def operator_leaks(root: Path) -> dict[str, set[str]]:
    """Modules outside cli / identity that name the operator or import it."""
    modules = parse_package(root)
    graph = import_graph(root)
    leaks: dict[str, set[str]] = {}
    for name, (tree, _) in modules.items():
        if allowed(name):
            continue
        reasons = set()
        if names_used(tree) & OPERATOR_NAMES:
            reasons.add("names the operator API")
        if OPERATOR_MODULE in import_closure(graph, name):
            reasons.add("imports the operator module")
        if reasons:
            leaks[name] = reasons
    return leaks


class NoWebPathTest(unittest.TestCase):
    def test_no_route_of_the_application_deals_with_owner_setup_or_recovery(self):
        paths = sorted(route_guards(build_app()))

        self.assertGreater(len(paths), 3)  # the inventory sees the real routes
        self.assertEqual([p for p in paths if SETUP_WORDS.search(p)], [])

    def test_visiting_a_likely_setup_url_creates_nothing_and_finds_nothing(self):
        client = make_client(build_app())

        for path in GUESSES:
            for method in ("GET", "POST", "PUT", "PATCH"):
                with self.subTest(path=path, method=method):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 404)

    def test_the_api_package_does_not_use_the_identity_service(self):
        sources = {path.name: path.read_text() for path in API_DIR.rglob("*.py")}

        self.assertIn("health.py", sources)
        for name, source in sources.items():
            with self.subTest(name):
                self.assertNotIn("paw_backend.identity", source)
                self.assertNotIn("OwnerOperator", source)


class OperatorIsolationTest(unittest.TestCase):
    def test_only_the_cli_and_the_identity_package_reach_the_operator(self):
        self.assertEqual(operator_leaks(BACKEND_DIR), {})

    def test_the_cli_does_use_the_operator(self):
        # The check is not vacuous: the one allowed user is found.
        modules = parse_package(BACKEND_DIR)
        tree, is_package = modules["paw_backend.cli.owner"]
        imported = imports_of(tree, "paw_backend.cli.owner", is_package, set(modules))

        self.assertIn(OPERATOR_MODULE, imported)
        self.assertTrue(names_used(tree) & OPERATOR_NAMES)

    def test_importing_the_identity_package_does_not_bring_the_operator_in(self):
        # PAW-022 imports ``paw_backend.identity`` (TokenRedeemer): that must
        # not drag the operator into the web application.
        graph = import_graph(BACKEND_DIR)
        identity_modules = [
            name
            for name in graph
            if name.startswith("paw_backend.identity") and name != OPERATOR_MODULE
        ]

        self.assertIn("paw_backend.identity", identity_modules)
        for name in identity_modules:
            with self.subTest(name):
                self.assertNotIn(OPERATOR_MODULE, import_closure(graph, name))

    def test_the_application_itself_does_not_reach_the_operator(self):
        graph = import_graph(BACKEND_DIR)

        for name in ("paw_backend", "paw_backend.app", "paw_backend.server"):
            with self.subTest(name):
                self.assertNotIn(OPERATOR_MODULE, import_closure(graph, name))
        # ... while the application does import the identity package (diagnostics).
        self.assertIn("paw_backend.identity", import_closure(graph, "paw_backend.app"))


class LeakDetectionTest(unittest.TestCase):
    """The detector itself, on synthetic trees (it must catch the review's route)."""

    BASE = {
        "paw_backend/__init__.py": "",
        "paw_backend/identity/__init__.py": (
            "from paw_backend.identity.redeemer import TokenRedeemer\n"
        ),
        "paw_backend/identity/redeemer.py": "class TokenRedeemer: ...\n",
        "paw_backend/identity/operator.py": (
            "class OwnerOperator:\n    async def setup_owner(self, name): ...\n"
        ),
        "paw_backend/api/__init__.py": "",
        "paw_backend/api/v1/__init__.py": "",
        "paw_backend/util/__init__.py": "",
    }

    def leaks(self, files: dict[str, str]) -> dict[str, set[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, source in {**self.BASE, **files}.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            return operator_leaks(root)

    def test_a_clean_tree_has_no_leak(self):
        route = (
            "from paw_backend.identity import TokenRedeemer\n"
            "async def route(token):\n"
            "    return await TokenRedeemer().redeem(token)\n"
        )

        self.assertEqual(self.leaks({"paw_backend/api/v1/login.py": route}), {})

    def test_a_bootstrap_route_calling_setup_owner_through_a_helper_is_found(self):
        # The review's example: POST /api/v1/bootstrap/first-user -> helper -> setup.
        leaks = self.leaks(
            {
                "paw_backend/api/v1/bootstrap.py": (
                    "from paw_backend.util.helper import first_user\n"
                    "async def route(name):\n    return await first_user(name)\n"
                ),
                "paw_backend/util/helper.py": (
                    "from paw_backend.identity.operator import OwnerOperator\n"
                    "async def first_user(name):\n"
                    "    return await OwnerOperator(None, None).setup_owner(name)\n"
                ),
            }
        )

        self.assertEqual(
            leaks,
            {
                "paw_backend.api.v1.bootstrap": {"imports the operator module"},
                "paw_backend.util.helper": {
                    "names the operator API",
                    "imports the operator module",
                },
            },
        )

    def test_a_relative_import_chain_is_followed(self):
        leaks = self.leaks(
            {
                "paw_backend/api/v1/bootstrap.py": "from ...util import helper\n",
                "paw_backend/util/helper.py": "from ..identity import operator\n",
            }
        )

        self.assertEqual(
            set(leaks), {"paw_backend.api.v1.bootstrap", "paw_backend.util.helper"}
        )

    def test_a_call_by_name_is_found_without_any_import(self):
        leaks = self.leaks(
            {
                "paw_backend/api/v1/sneaky.py": (
                    "async def route(service, name):\n"
                    "    return await service.setup_owner(name)\n"
                ),
                "paw_backend/api/v1/reflective.py": (
                    "async def route(service):\n"
                    "    return await getattr(service, 'recover_owner')()\n"
                ),
            }
        )

        self.assertEqual(
            set(leaks), {"paw_backend.api.v1.sneaky", "paw_backend.api.v1.reflective"}
        )

    def test_a_package_that_imports_the_operator_taints_whoever_imports_it(self):
        leaks = self.leaks(
            {
                "paw_backend/identity/__init__.py": (
                    "from paw_backend.identity.operator import OwnerOperator\n"
                ),
                "paw_backend/api/v1/login.py": (
                    "from paw_backend.identity import redeemer\n"
                ),
            }
        )

        self.assertEqual(
            leaks, {"paw_backend.api.v1.login": {"imports the operator module"}}
        )

    def test_the_cli_and_the_identity_package_may_use_the_operator(self):
        self.assertEqual(
            self.leaks(
                {
                    "paw_backend/cli/__init__.py": "",
                    "paw_backend/cli/owner.py": (
                        "from paw_backend.identity.operator import OwnerOperator\n"
                        "def run(s):\n"
                        "    return OwnerOperator(None, None).setup_owner(s)\n"
                    ),
                }
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
