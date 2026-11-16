"""The model catalog PAW hands Codex (Decision 0104, 1 A and 2 A, as amended
by Decision 0105, 2 A; issue #341).

* the repository's ``0.156.1`` catalog is the Human's capture (with the
  dedicated login, 2026-11-16): its SHA-256, its models (no ``gpt-5.5``);
* the override sets exactly the five values on the session model's entry and
  nothing else (the other values, the other models, the given catalog);
* every verified version has a catalog, and every verified Codex pair's model
  is in it and overrides cleanly;
* fail closed: a model not in the catalog, a Code Mode or multi-agent value
  left after the override (``gpt-6-astra``'s ``multi_agent_reasoning_effort``
  included), a missing, unreadable or malformed catalog.
"""

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from paw_backend.agent_models import VERIFIED_MODELS
from paw_backend.agents.codex_app_server import VERIFIED_VERSIONS
from paw_backend.agents.codex_catalog import (
    CATALOG_DIR,
    OVERRIDES,
    REPOSITORY_CATALOGS,
    CatalogRefused,
    CodexCatalogs,
    catalog_path,
    check_entry,
    overridden,
    parse_catalog,
    render,
)
from paw_backend.connections import ConnectionKind

from .codex_catalog_support import CODE_MODE_ENTRY, fake_catalog

#: ``codex debug models`` of codex-cli 0.156.1 with the dedicated ChatGPT
#: login (``/data/results/paw-codex-live-check-2026-11-16/catalog-login.json``):
#: a change of the file is a change of what every session gets.
CAPTURE_SHA256 = "990e2273ad7e49ae3b58088e0b5736d84b59bfef9f10e9bacd82d97dcad25a31"
EXPECTED = {
    "tool_mode": "direct",
    "multi_agent_version": None,
    "experimental_supported_tools": [],
    "use_responses_lite": False,
    "supports_search_tool": False,
}


def refused(test, code, call, *args):
    with test.assertRaises(CatalogRefused) as caught:
        call(*args)
    test.assertEqual(caught.exception.code, code)


class RepositoryCatalogTest(unittest.TestCase):
    def setUp(self):
        self.path = catalog_path("0.156.1")
        self.catalog = parse_catalog(self.path.read_bytes())

    def test_it_is_the_humans_capture_inside_the_package(self):
        self.assertEqual(self.path.parent, CATALOG_DIR)
        self.assertEqual(self.path.name, "codex-models-0.156.1.json")
        self.assertTrue(
            self.path.is_relative_to(Path(__file__).parents[1] / "paw_backend")
        )
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(digest, CAPTURE_SHA256)

    def test_its_models_are_the_logged_in_ones(self):
        slugs = [entry["slug"] for entry in self.catalog["models"]]
        self.assertEqual(
            slugs,
            [
                "gpt-6-astra",
                "gpt-6-sol",
                "gpt-reserve",
                "gpt-6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "codex-auto-review",
            ],
        )
        # A ChatGPT account cannot use gpt-5.5 (Decision 0105): not there.
        self.assertNotIn("gpt-5.5", slugs)
        for entry in self.catalog["models"]:
            self.assertEqual(entry["tool_mode"], "code_mode_only", entry["slug"])

    def test_gpt_6_sol_gets_the_five_values_and_keeps_the_rest(self):
        original = copy.deepcopy(self.catalog)
        result = overridden(self.catalog, "gpt-6-sol")
        self.assertEqual(self.catalog, original)  # the given one is unchanged
        (before,) = [e for e in original["models"] if e["slug"] == "gpt-6-sol"]
        (after,) = [e for e in result["models"] if e["slug"] == "gpt-6-sol"]
        self.assertEqual(before["tool_mode"], "code_mode_only")
        self.assertEqual(before["multi_agent_version"], "v2")
        self.assertEqual({k: after[k] for k in EXPECTED}, EXPECTED)
        self.assertEqual(set(after), set(before))
        for key in set(before) - set(EXPECTED):
            self.assertEqual(after[key], before[key], key)
        # The login catalog's values the Human chose (Decision 0105, 2 A).
        self.assertIs(after["supports_experimental_context"], True)
        self.assertIs(after["supports_reasoning_effort_updates"], True)
        self.assertEqual(after["description"], "Previous generation workhorse model.")
        # Every other model as captured.
        others = [e for e in result["models"] if e["slug"] != "gpt-6-sol"]
        self.assertEqual(
            others, [e for e in original["models"] if e["slug"] != "gpt-6-sol"]
        )
        self.assertEqual(set(result), set(original))

    def test_the_session_catalog_is_that_as_json(self):
        text = REPOSITORY_CATALOGS.session_catalog("0.156.1", "gpt-6-sol")
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text), overridden(self.catalog, "gpt-6-sol"))
        self.assertIs(REPOSITORY_CATALOGS.session_catalog("0.156.1", "gpt-6-sol"), text)

    def test_gpt_5_5_is_not_in_it(self):
        refused(
            self, "model_not_in_catalog", REPOSITORY_CATALOGS.session_catalog,
            "0.156.1", "gpt-5.5",
        )  # fmt: skip

    def test_gpt_6_astra_keeps_a_multi_agent_value(self):
        # Its entry has ``multi_agent_reasoning_effort = "xhigh"``, which the
        # five values do not cover: fail closed (a Human's decision).
        (astra,) = [e for e in self.catalog["models"] if e["slug"] == "gpt-6-astra"]
        self.assertEqual(astra["multi_agent_reasoning_effort"], "xhigh")
        refused(self, "code_mode_left", overridden, self.catalog, "gpt-6-astra")

    def test_every_verified_version_and_pair_has_its_catalog(self):
        for version in VERIFIED_VERSIONS:
            with self.subTest(version):
                self.assertTrue(catalog_path(version).is_file())
                REPOSITORY_CATALOGS.catalog(version)
        for version, model in VERIFIED_MODELS[ConnectionKind.CODEX]:
            with self.subTest((version, model)):
                REPOSITORY_CATALOGS.session_catalog(version, model)


class OverrideTest(unittest.TestCase):
    def test_exactly_the_five_values(self):
        self.assertEqual(
            {k: list(v) if isinstance(v, tuple) else v for k, v in OVERRIDES.items()},
            EXPECTED,
        )
        with self.assertRaises(TypeError):
            OVERRIDES["shell_type"] = None

    def test_only_the_models_entry_changes(self):
        catalog = fake_catalog(("a", "b"))
        result = overridden(catalog, "b")
        self.assertEqual(result["models"][0], {"slug": "a", **CODE_MODE_ENTRY})
        self.assertEqual(result["models"][1]["tool_mode"], "direct")
        self.assertEqual(
            result["models"][1]["base_instructions"], "You are a test model."
        )
        self.assertEqual(catalog["models"][1]["tool_mode"], "code_mode_only")

    def test_missing_keys_are_set(self):
        result = overridden({"models": [{"slug": "m"}]}, "m")
        self.assertEqual(result["models"][0], {"slug": "m", **EXPECTED})

    def test_a_model_not_in_it_is_refused(self):
        refused(self, "model_not_in_catalog", overridden, fake_catalog(("a",)), "b")

    def test_a_code_mode_value_left_is_refused(self):
        for name, extra in {
            "a code_mode key": {"code_mode_host": False},
            "a code_mode value": {"shell_type": "code_mode"},
            "another multi-agent value": {"multi_agent_reasoning_effort": "xhigh"},
            "a multi_agent key": {"default_multi_agent": "v1"},
        }.items():
            with self.subTest(name):
                catalog = {"models": [{"slug": "m", **CODE_MODE_ENTRY, **extra}]}
                refused(self, "code_mode_left", overridden, catalog, "m")

    def test_a_null_multi_agent_value_passes(self):
        catalog = {"models": [{"slug": "m", "multi_agent_reasoning_effort": None}]}
        overridden(catalog, "m")

    def test_the_check_needs_every_value(self):
        for key in EXPECTED:
            with self.subTest(key):
                entry = {"slug": "m", **EXPECTED}
                del entry[key]
                refused(self, "code_mode_left", check_entry, entry)
                entry[key] = "code_mode_only"
                refused(self, "code_mode_left", check_entry, entry)

    def test_render_is_one_json_line(self):
        text = render({"models": [{"slug": "m", "x": "é"}]})
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(json.loads(text)["models"][0]["x"], "é")


class MalformedTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, text, version="1.2.3"):
        path = self.directory / f"codex-models-{version}.json"
        path.write_text(text)
        return CodexCatalogs(self.directory)

    def test_malformed_catalogs_are_refused(self):
        for name, text in {
            "not JSON": "{",
            "a list": "[]",
            "no models": "{}",
            "models not a list": '{"models": {}}',
            "an entry not an object": '{"models": [1]}',
            "no slug": '{"models": [{}]}',
            "an empty slug": '{"models": [{"slug": ""}]}',
            "a slug not a string": '{"models": [{"slug": 1}]}',
            "a slug twice": '{"models": [{"slug": "m"}, {"slug": "m"}]}',
            "a key twice": '{"models": [{"slug": "m", "tool_mode": "direct",'
            ' "tool_mode": "code_mode_only"}]}',
            "NaN": '{"models": [{"slug": "m", "context_window": NaN}]}',
        }.items():
            with self.subTest(name):
                catalogs = self.write(text)
                refused(self, "malformed", catalogs.session_catalog, "1.2.3", "m")

    def test_a_too_large_file_is_refused(self):
        catalogs = self.write(" " * (8 * 1024 * 1024 + 1))
        refused(self, "malformed", catalogs.session_catalog, "1.2.3", "m")

    def test_a_missing_file_is_refused(self):
        catalogs = CodexCatalogs(self.directory)
        refused(self, "missing", catalogs.session_catalog, "1.2.3", "m")
        refused(self, "missing", CodexCatalogs(catalogs={}).catalog, "1.2.3")

    def test_an_unreadable_file_is_refused(self):
        (self.directory / "codex-models-1.2.3.json").mkdir()
        catalogs = CodexCatalogs(self.directory)
        refused(self, "unreadable", catalogs.session_catalog, "1.2.3", "m")

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_a_file_without_read_permission_is_refused(self):
        catalogs = self.write('{"models": [{"slug": "m"}]}')
        os.chmod(self.directory / "codex-models-1.2.3.json", 0)
        refused(self, "unreadable", catalogs.session_catalog, "1.2.3", "m")

    def test_a_version_that_is_not_one_names_no_file(self):
        for version in ("../1.2.3", "1.2", "", "1.2.3/x", None):
            with self.subTest(version):
                refused(self, "missing", catalog_path, version)

    def test_a_refusal_is_not_kept(self):
        catalogs = self.write("{")
        refused(self, "malformed", catalogs.catalog, "1.2.3")
        self.write('{"models": [{"slug": "m"}]}')
        self.assertEqual(catalogs.catalog("1.2.3"), {"models": [{"slug": "m"}]})


if __name__ == "__main__":
    unittest.main()
