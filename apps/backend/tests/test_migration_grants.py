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


def _callee(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else ""


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def unguarded_tables(source: str) -> list[str]:
    """Problems that stop a migration from granting the application role."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    created: list[str] = []
    problems: list[str] = []
    granted: set[str] = set()
    opted_out: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = _callee(node)
            first = _literal(
                node.args[1] if callee == HELPER and len(node.args) > 1 else None
            )
            if callee == "create_table":
                name = _literal(node.args[0] if node.args else None)
                if name is None:
                    problems.append(
                        "create_table with a table name that is not a literal"
                    )
                else:
                    created.append(name)
            elif callee == HELPER:
                if first is None:
                    problems.append(f"{HELPER} with a table name that is not a literal")
                else:
                    granted.add(first)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings  # prose may say "CREATE TABLE"
        ):
            created.extend(_RAW_CREATE.findall(node.value))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == OPT_OUT
            for target in node.targets
        ):
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

    for table in dict.fromkeys(created):
        if table not in granted and table not in opted_out:
            problems.append(f"table {table!r} is created without {HELPER}")
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


class LintDetectsTest(unittest.TestCase):
    """The rule itself: it must fail for each way of forgetting the grant."""

    def check(self, body: str) -> list[str]:
        return unguarded_tables("import sqlalchemy as sa\n" + body)

    def test_a_table_without_a_grant_is_reported(self):
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets', sa.Column('id', sa.Uuid()))\n"
        )
        self.assertEqual(
            problems, ["table 'widgets' is created without grant_app_privileges"]
        )

    def test_a_grant_for_the_table_satisfies_the_rule(self):
        self.assertEqual(
            self.check(
                "def upgrade():\n"
                "    op.create_table('widgets')\n"
                "    grant_app_privileges(op, 'widgets', insert=True)\n"
            ),
            [],
        )
        self.assertEqual(
            self.check(
                "def upgrade():\n"
                "    op.create_table('widgets')\n"
                "    db_roles.grant_app_privileges(op, 'widgets')\n"
            ),
            [],
        )

    def test_a_grant_for_another_table_does_not_count(self):
        problems = self.check(
            "def upgrade():\n"
            "    op.create_table('widgets')\n"
            "    op.create_table('gadgets')\n"
            "    grant_app_privileges(op, 'widgets')\n"
        )
        self.assertEqual(
            problems, ["table 'gadgets' is created without grant_app_privileges"]
        )

    def test_every_table_of_a_migration_needs_its_own_grant(self):
        problems = self.check(
            "def upgrade():\n    op.create_table('a')\n    op.create_table('b')\n"
        )
        self.assertEqual(len(problems), 2)

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
        self.assertIn("table 'a' is created without grant_app_privileges", problems)

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
                self.assertEqual(
                    problems,
                    ["table 'widgets' is created without grant_app_privileges"],
                )
        granted = self.check(
            "def upgrade():\n"
            "    op.execute('CREATE TABLE widgets (id int)')\n"
            "    grant_app_privileges(op, 'widgets')\n"
        )
        self.assertEqual(granted, [])

    def test_an_opt_out_needs_a_real_reason(self):
        ok = self.check(
            "NO_APP_GRANTS = {'widgets': 'only the migration role reads this table'}\n"
            "def upgrade():\n    op.create_table('widgets')\n"
        )
        self.assertEqual(ok, [])
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

    def test_prose_in_a_docstring_is_not_a_table(self):
        self.assertEqual(
            unguarded_tables(
                '"""Explains why CREATE TABLE widgets is not used here."""\n'
                'def upgrade():\n    """CREATE TABLE gadgets (id int)"""\n'
            ),
            [],
        )

    def test_a_migration_without_tables_needs_nothing(self):
        self.assertEqual(
            self.check(
                "def upgrade():\n    op.add_column('t', sa.Column('c', sa.Text()))\n"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
