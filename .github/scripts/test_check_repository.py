"""Acceptance/regression fixtures for the repository CI validator."""

from pathlib import Path
import tempfile
import unittest

import yaml

from check_repository import UniqueKeyLoader, validate


class RepositoryChecksTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def check(self, files):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return validate(self.root, [Path(name) for name in files])[1]

    def test_valid_documentation_and_workflow(self):
        errors = self.check({
            "README.md": "# Test\n\n[guide](docs/guide.md)\n",
            "docs/guide.md": (
                "[root](../README.md#test)\n[dir](../docs/)\n"
                "[root path](/README.md)\n![image](image%20file.svg)\n"
                "[ref][target]\n\n[target]: ../README.md \"title\"\n"
                "[external](https://example.invalid/)\n[CDN](//example.invalid/image)\n"
                "[email](mailto:example@example.invalid)\n[section](#test)\n"
                "`[code](missing.md)`\n\n```markdown\n[example](absent.md)\n```\n"
                "\nHard break  \nnext line\n\nHeading\n=======\n"
            ),
            "docs/image file.svg": "<svg/>\n",
            ".github/workflows/ci.yml": "on:\n  pull_request:\npermissions:\n  contents: read\n",
        })
        self.assertEqual(errors, [])

    def test_missing_links_images_and_reference_links(self):
        for link in (
            "[missing](absent.md)", "![missing](absent.png)",
            "[missing][ref]\n\n[ref]: absent.md",
            "| heading |\n| --- |\n| [missing](absent.md) |",
        ):
            with self.subTest(link=link):
                errors = self.check({"README.md": link + "\n"})
                self.assertTrue(any("target missing" in error for error in errors), errors)

    def test_links_cannot_escape_repository(self):
        errors = self.check({"README.md": "[outside](../outside.md)\n"})
        self.assertTrue(any("leaves repository" in error for error in errors), errors)

    def test_existing_untracked_link_targets_are_rejected(self):
        for name in ("untracked.md", ".git/config", ".github/scripts/__pycache__/cache.pyc"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("untracked\n", encoding="utf-8")
        for destination in ("untracked.md", ".git/", ".github/scripts/__pycache__/"):
            with self.subTest(destination=destination):
                errors = self.check({"README.md": f"[link]({destination})\n"})
                self.assertTrue(any("not tracked" in error for error in errors), errors)

    def test_tracked_symlink_cannot_link_to_untracked_content(self):
        (self.root / "untracked.md").write_text("untracked\n", encoding="utf-8")
        (self.root / "alias.md").symlink_to("untracked.md")
        (self.root / "README.md").write_text("[link](alias.md)\n", encoding="utf-8")
        _, errors = validate(self.root, [Path("README.md"), Path("alias.md")])
        self.assertTrue(any("not tracked" in error for error in errors), errors)

    def test_symlinked_sources_are_not_read_outside_repository(self):
        (self.root / "README.md").symlink_to(self.root.parent / "outside.md")
        _, errors = validate(self.root, [Path("README.md")])
        self.assertEqual(errors, ["README.md: file leaves repository"])

    def test_yaml_syntax_and_nested_duplicate_keys_are_rejected(self):
        for content, expected in (
            ("key: [unterminated\n", "YAML"),
            ("jobs:\n  check:\n    name: first\n    name: second\n", "duplicate mapping key"),
            ("on: push\non: pull_request\n", "duplicate mapping key"),
            ("job:\n  <<: {a: 1}\n  <<: {b: 2}\n", "duplicate mapping key"),
            ("job:\n  <<:\n    x: 1\n    x: 2\n", "duplicate mapping key"),
            ("x: !!python/object/apply:os.system ['exit 0']\n", "YAML"),
        ):
            with self.subTest(content=content):
                errors = self.check({"config.yml": content})
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_yaml_on_is_string_and_safe_loader_is_unchanged(self):
        result = yaml.load("on: push\ntrue: yes\n", Loader=UniqueKeyLoader)
        self.assertEqual(result, {"on": "push", True: "yes"})
        self.assertEqual(yaml.safe_load("on: push\n"), {True: "push"})

    def test_yaml_merge_overrides_are_valid(self):
        errors = self.check({"config.yml": (
            "defaults: &defaults\n  timeout: 10\njob:\n"
            "  <<: *defaults\n  timeout: 20\n"
        )})
        self.assertEqual(errors, [])

    def test_yaml_nested_merge_alias_is_not_mistaken_for_duplicate_key(self):
        errors = self.check({"config.yml": (
            "a: &a\n  x: a\nb:\n  <<: &b\n    <<: *a\n    x: b\nc: *b\n"
        )})
        self.assertEqual(errors, [])

    def test_yaml_exponential_merge_expansion_is_rejected_before_allocation(self):
        content = "n0: &n0 {x: 1}\n" + "".join(
            f"n{i}: &n{i} {{<<: [*n{i-1}, *n{i-1}]}}\n" for i in range(1, 15)
        )
        loader = UniqueKeyLoader(content)
        self.addCleanup(loader.dispose)
        root = loader.get_single_node()
        final_mapping = root.value[-1][1]
        with self.assertRaisesRegex(yaml.constructor.ConstructorError, "exceeds 10000"):
            loader.construct_document(root)
        # The vulnerable loader allocated 16,384 entries here from 383 bytes.
        self.assertEqual(len(final_mapping.value), 1)

    def test_yaml_merge_expansion_boundary_and_cycles(self):
        base = "n0: &n0 {x: 1}\n" + "".join(
            f"n{i}: &n{i} {{<<: [*n{i-1}, *n{i-1}]}}\n" for i in range(1, 14)
        )
        for extra, expected in (("", False), (", *n0", True)):
            with self.subTest(expanded_entries=10000 + bool(extra)):
                content = base + f"limit: {{<<: [*n13, *n10, *n9, *n8, *n4{extra}]}}\n"
                errors = self.check({"config.yml": content})
                self.assertEqual(bool(errors), expected, errors)
                if expected:
                    self.assertIn("exceeds 10000", errors[0])
        errors = self.check({"config.yml": "node: &node {<<: *node}\n"})
        self.assertTrue(any("recursive YAML merge" in error for error in errors), errors)

    def test_trailing_whitespace_and_conflict_markers_are_rejected(self):
        for name, content, expected in (
            ("README.md", "single space \n", "trailing whitespace"),
            ("README.md", "tab\t\n", "trailing whitespace"),
            ("README.md", "  \n", "trailing whitespace"),
            ("config.yml", "name: value  \n", "trailing whitespace"),
            ("README.md", "<<<<<<< HEAD\n", "merge conflict marker"),
            ("README.md", ">>>>>>> other\n", "merge conflict marker"),
            ("README.md", "||||||| base\n", "merge conflict marker"),
        ):
            with self.subTest(content=content):
                errors = self.check({name: content})
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_untracked_files_are_not_implicitly_scanned(self):
        (self.root / "untracked.md").write_text("bad whitespace \n", encoding="utf-8")
        self.assertEqual(self.check({"README.md": "# Valid\n"}), [])


if __name__ == "__main__":
    unittest.main()
