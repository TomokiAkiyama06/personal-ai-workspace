"""Every migration that creates a table must grant the application role.

In the split-role deployment tables belong to the migration role and the
backend connects as another role, so a table without a grant is unusable
(``permission denied``). A version file therefore has to call
``grant_app_privileges(op, "<table>", ...)`` for each table it creates, or list
the table in a module-level ``NO_APP_GRANTS = {"<table>": "<why>"}`` with a
real reason (a table only the migration role touches, say).
"""

import ast
import re
import unittest
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
HELPER = "grant_app_privileges"
OPT_OUT = "NO_APP_GRANTS"
# `CREATE TABLE [IF NOT EXISTS] name` inside a raw SQL string.
_RAW_CREATE = re.compile(
    r"CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
    re.IGNORECASE,
)
MIN_REASON = 15

# Code under these nodes is not certain to run when its parent does: a branch,
# a loop, a handler, a short-circuit, or a body that only runs when called.
_MAY_NOT_RUN = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.Match,
    ast.IfExp,
    ast.BoolOp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
)


def _callee(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else ""


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _table_argument(node: ast.Call) -> ast.AST | None:
    """The ``table`` of ``grant_app_privileges(op, table, ...)``."""
    if len(node.args) > 1:
        return node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "table":
            return keyword.value
    return None


class _UpgradePath:
    """What ``upgrade()`` creates and what it certainly grants.

    Only ``upgrade()`` and the module functions it calls (resolved by name,
    within the module) count; ``downgrade()``, unused helpers and dead code do
    not. A grant counts only when it is certain to run: a call inside a branch,
    loop, ``try``, conditional expression, nested function or a helper that is
    itself only called conditionally is not enough (the helper function already
    does nothing when no application role is configured, so there is no reason
    to guard the call).
    """

    def __init__(self, tree: ast.Module) -> None:
        self.functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.created: list[str] = []
        self.granted: set[str] = set()
        self.problems: list[str] = []
        self._visited: set[tuple[str, bool]] = set()
        self._docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }

    def analyse(self) -> None:
        upgrade = self.functions.get("upgrade")
        if upgrade is None:
            self.problems.append("the migration has no upgrade() function")
            return
        self._function(upgrade, conditional=False)

    def _function(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, conditional: bool
    ):
        key = (function.name, conditional)
        if key in self._visited:
            return
        self._visited.add(key)
        for statement in function.body:
            self._visit(statement, conditional)

    def _visit(self, node: ast.AST, conditional: bool) -> None:
        if isinstance(node, ast.Call):
            self._call(node, conditional)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in self._docstrings:  # prose may say "CREATE TABLE"
                self.created.extend(_RAW_CREATE.findall(node.value))
        elif isinstance(node, ast.Name) and node.id in self.constants:
            # A module-level SQL constant that the upgrade path uses.
            self.created.extend(_RAW_CREATE.findall(self.constants[node.id]))
        below = conditional or isinstance(node, _MAY_NOT_RUN)
        for child in ast.iter_child_nodes(node):
            self._visit(child, below)

    def _call(self, node: ast.Call, conditional: bool) -> None:
        callee = _callee(node)
        if callee == "create_table":
            name = _literal(node.args[0] if node.args else None)
            if name is None:
                self.problems.append(
                    "create_table with a table name that is not a literal"
                )
            else:
                self.created.append(name)
        elif callee == HELPER:
            name = _literal(_table_argument(node))
            if name is None:
                self.problems.append(
                    f"{HELPER} with a table name that is not a literal"
                )
            elif not conditional:
                self.granted.add(name)
        elif isinstance(node.func, ast.Name) and node.func.id in self.functions:
            self._function(self.functions[node.func.id], conditional)


def _opt_outs(tree: ast.Module, problems: list[str]) -> dict[str, str]:
    opted_out: dict[str, str] = {}
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == OPT_OUT for t in node.targets)
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            problems.append(f"{OPT_OUT} must be a dict literal")
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            table, reason = _literal(key), _literal(value)
            if table is None or reason is None or len(reason.strip()) < MIN_REASON:
                problems.append(
                    f"{OPT_OUT} entries need a table name and a real reason"
                )
            else:
                opted_out[table] = reason
    return opted_out


def unguarded_tables(source: str) -> list[str]:
    """Problems that stop ``upgrade()`` from granting the application role."""
    tree = ast.parse(source)
    path = _UpgradePath(tree)
    path.analyse()
    problems = list(path.problems)
    opted_out = _opt_outs(tree, problems)
    for table in dict.fromkeys(path.created):
        if table not in path.granted and table not in opted_out:
            problems.append(f"table {table!r} is created in upgrade() without {HELPER}")
    return problems


class MigrationGrantRuleTest(unittest.TestCase):
    def test_every_migration_that_creates_a_table_grants_the_app_role(self):
        files = sorted(VERSIONS.glob("*.py"))
        self.assertTrue(files, "no migrations found")
        for path in files:
            with self.subTest(migration=path.name):
                self.assertEqual(unguarded_tables(path.read_text()), [])

    def test_the_audit_migration_is_covered_by_the_rule(self):
        # Guards the rule against silently checking nothing.
        source = (VERSIONS / "0025_audit_events.py").read_text()
        self.assertIn('create_table(\n        "audit_events"', source)
        self.assertIn(HELPER + '(op, "audit_events"', source)


def missing(table: str) -> str:
    return f"table {table!r} is created in upgrade() without grant_app_privileges"


class LintDetectsTest(unittest.TestCase):
    """The rule itself: it must fail for each way of forgetting the grant."""

    def check(self, body: str) -> list[str]:
        return unguarded_tables("import sqlalchemy as sa\n" + body)

    def test_a_table_without_a_grant_is_reported(self):
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets', sa.Column('id', sa.Uuid()))\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_a_grant_in_upgrade_satisfies_the_rule(self):
        for call in (
            "grant_app_privileges(op, 'widgets', insert=True)",
            "db_roles.grant_app_privileges(op, 'widgets')",
            "grant_app_privileges(op, table='widgets')",
        ):
            with self.subTest(call=call):
                self.assertEqual(
                    self.check(
                        f"def upgrade():\n    op.create_table('widgets')\n    {call}\n"
                    ),
                    [],
                )

    def test_a_grant_before_or_inside_a_with_block_still_counts(self):
        self.assertEqual(
            self.check(
                "def upgrade():\n"
                "    op.create_table('widgets')\n"
                "    with op.batch_alter_table('widgets'):\n"
                "        grant_app_privileges(op, 'widgets')\n"
            ),
            [],
        )

    def test_a_grant_in_an_upgrade_called_helper_satisfies_the_rule(self):
        self.assertEqual(
            self.check(
                "def _grant():\n"
                "    grant_app_privileges(op, 'widgets')\n"
                "def upgrade():\n"
                "    op.create_table('widgets')\n"
                "    _grant()\n"
            ),
            [],
        )
        # ... and through a chain of helpers.
        self.assertEqual(
            self.check(
                "def _inner():\n    grant_app_privileges(op, 'widgets')\n"
                "def _outer():\n    _inner()\n"
                "def upgrade():\n    op.create_table('widgets')\n    _outer()\n"
            ),
            [],
        )

    def test_a_grant_only_in_downgrade_does_not_count(self):
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "def downgrade():\n"
            "    grant_app_privileges(op, 'widgets')\n"
            "    op.drop_table('widgets')\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_a_grant_in_an_unused_helper_does_not_count(self):
        problems = self.check(
            "def _grant():\n"
            "    grant_app_privileges(op, 'widgets')\n"
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
        )
        self.assertEqual(problems, [missing("widgets")])
        # A helper only downgrade() calls is just as unused for the upgrade.
        problems = self.check(
            "def _grant():\n"
            "    grant_app_privileges(op, 'widgets')\n"
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "def downgrade():\n"
            "    _grant()\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_a_grant_for_another_table_does_not_count(self):
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "    op.create_table('gadgets')\n"
            "    grant_app_privileges(op, 'widgets')\n"
        )
        self.assertEqual(problems, [missing("gadgets")])
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "    grant_app_privileges(op, 'widget')\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_a_grant_that_may_not_run_does_not_count(self):
        cases = {
            "if": "    if False:\n        grant_app_privileges(op, 'widgets')\n",
            "if-else": (
                "    if FLAG:\n        grant_app_privileges(op, 'widgets')\n"
                "    else:\n        pass\n"
            ),
            "for": "    for _ in []:\n        grant_app_privileges(op, 'widgets')\n",
            "while": "    while False:\n        grant_app_privileges(op, 'widgets')\n",
            "try": (
                "    try:\n        grant_app_privileges(op, 'widgets')\n"
                "    except Exception:\n        pass\n"
            ),
            "expression": "    FLAG and grant_app_privileges(op, 'widgets')\n",
            "ternary": (
                "    x = grant_app_privileges(op, 'widgets') if FLAG else None\n"
            ),
            "nested def": (
                "    def later():\n        grant_app_privileges(op, 'widgets')\n"
            ),
            "lambda": "    later = lambda: grant_app_privileges(op, 'widgets')\n",
        }
        for name, block in cases.items():
            with self.subTest(case=name):
                problems = self.check(
                    "def upgrade():\n    op.create_table('widgets')\n" + block
                )
                self.assertEqual(problems, [missing("widgets")])

    def test_a_helper_called_only_conditionally_does_not_count(self):
        problems = self.check(
            "def _grant():\n"
            "    grant_app_privileges(op, 'widgets')\n"
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "    if FLAG:\n"
            "        _grant()\n"
        )
        self.assertEqual(problems, [missing("widgets")])
        # ... unless upgrade() also calls it unconditionally.
        self.assertEqual(
            self.check(
                "def _grant():\n"
                "    grant_app_privileges(op, 'widgets')\n"
                "def upgrade():\n"
                "    op.create_table('widgets')\n"
                "    if FLAG:\n"
                "        _grant()\n"
                "    _grant()\n"
            ),
            [],
        )

    def test_a_table_created_under_a_condition_still_needs_its_grant(self):
        problems = self.check(
            "def upgrade():\n    if FLAG:\n        op.create_table('widgets')\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_a_table_created_in_downgrade_is_irrelevant(self):
        self.assertEqual(
            self.check(
                "def upgrade():\n"
                "    op.drop_table('widgets')\n"
                "def downgrade():\n"
                "    op.create_table('widgets')\n"
                "    op.execute('CREATE TABLE gadgets (id int)')\n"
            ),
            [],
        )

    def test_a_table_created_by_a_helper_upgrade_calls_is_found(self):
        problems = self.check(
            "def _create():\n"
            "    op.create_table('widgets')\n"
            "def upgrade():\n"
            "    _create()\n"
        )
        self.assertEqual(problems, [missing("widgets")])
        problems = self.check(
            "def _create():\n"
            "    op.create_table('widgets')\n"
            "def upgrade():\n"
            "    _create()\n"
            "    grant_app_privileges(op, 'widgets')\n"
        )
        self.assertEqual(problems, [])

    def test_every_table_of_a_migration_needs_its_own_grant(self):
        problems = self.check(
            "def upgrade():\n    op.create_table('a')\n    op.create_table('b')\n"
        )
        self.assertEqual(problems, [missing("a"), missing("b")])

    def test_a_table_name_that_is_not_a_literal_cannot_be_checked(self):
        self.assertEqual(
            len(self.check("def upgrade():\n    op.create_table(NAME)\n")), 1
        )
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('a')\n"
            "    grant_app_privileges(op, NAME)\n"
        )
        self.assertIn(
            "grant_app_privileges with a table name that is not a literal", problems
        )
        self.assertIn(missing("a"), problems)

    def test_raw_sql_that_creates_a_table_is_caught_too(self):
        for statement in (
            "CREATE TABLE widgets (id int)",
            "create table if not exists widgets (id int)",
            'CREATE TABLE "widgets" (id int)',
            "CREATE UNLOGGED TABLE public.widgets (id int)",
        ):
            with self.subTest(statement=statement):
                problems = self.check(
                    f"def upgrade():\n    op.execute({statement!r})\n"
                )
                self.assertEqual(problems, [missing("widgets")])
        granted = self.check(
            "def upgrade():\n"
            "    op.execute('CREATE TABLE widgets (id int)')\n"
            "    grant_app_privileges(op, 'widgets')\n"
        )
        self.assertEqual(granted, [])

    def test_raw_sql_kept_in_a_module_constant_is_found_when_upgrade_uses_it(self):
        used = self.check(
            "_SQL = 'CREATE TABLE widgets (id int)'\n"
            "def upgrade():\n    op.execute(_SQL)\n"
        )
        self.assertEqual(used, [missing("widgets")])
        # Used only by downgrade (to recreate what it dropped): irrelevant.
        unused = self.check(
            "_SQL = 'CREATE TABLE widgets (id int)'\n"
            "def upgrade():\n    pass\n"
            "def downgrade():\n    op.execute(_SQL)\n"
        )
        self.assertEqual(unused, [])

    def test_an_opt_out_with_a_reason_satisfies_the_rule(self):
        ok = self.check(
            "NO_APP_GRANTS = {'widgets': 'only the migration role reads this table'}\n"
            "def upgrade():\n    op.create_table('widgets')\n"
        )
        self.assertEqual(ok, [])

    def test_an_opt_out_needs_a_real_reason(self):
        for reason in ("''", "'todo'", "'x' * 30", "None"):
            with self.subTest(reason=reason):
                problems = self.check(
                    f"NO_APP_GRANTS = {{'widgets': {reason}}}\n"
                    "def upgrade():\n    op.create_table('widgets')\n"
                )
                self.assertTrue(problems)
        not_a_dict = self.check(
            "NO_APP_GRANTS = build()\ndef upgrade():\n    op.create_table('widgets')\n"
        )
        self.assertIn("NO_APP_GRANTS must be a dict literal", not_a_dict)

    def test_an_opt_out_for_another_table_does_not_cover_this_one(self):
        problems = self.check(
            "NO_APP_GRANTS = {'gadgets': 'only the migration role reads this table'}\n"
            "def upgrade():\n    op.create_table('widgets')\n"
        )
        self.assertEqual(problems, [missing("widgets")])

    def test_prose_in_a_docstring_is_not_a_table(self):
        self.assertEqual(
            unguarded_tables(
                '"""Explains why CREATE TABLE widgets is not used here."""\n'
                'def upgrade():\n    """CREATE TABLE gadgets (id int)"""\n'
            ),
            [],
        )

    def test_a_migration_without_an_upgrade_is_reported(self):
        problems = self.check("def downgrade():\n    op.create_table('widgets')\n")
        self.assertEqual(problems, ["the migration has no upgrade() function"])

    def test_a_migration_without_tables_needs_nothing(self):
        self.assertEqual(
            self.check(
                "def upgrade():\n    op.add_column('t', sa.Column('c', sa.Text()))\n"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
