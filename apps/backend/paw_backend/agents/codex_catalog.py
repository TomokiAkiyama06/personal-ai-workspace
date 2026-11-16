"""The model catalog PAW hands Codex (Decision 0104, 1 A, 2 A and 5 A, as
amended by Decision 0105, 2 A; approved 2026-11-16; issue #341).

``codex-cli 0.156.1``'s catalog models (``gpt-6-sol`` and the others) are all
``tool_mode = "code_mode_only"`` with ``multi_agent_version`` set: Codex then
offers them ``exec``, ``wait``, the ``collaboration`` tools and others in an
``additional_tools`` input item, outside Decision 0101's allowed tools, and
the features of ``DISABLED_FEATURES`` do not remove them (the catalog's values
win). Codex reads a replacement catalog from the file that ``config.toml``'s
top-level ``model_catalog_json`` names. So every Codex session (a node's and
the connection's health probe, Decision 0104, 5 A) gets the repository's
catalog of its CLI version with :data:`OVERRIDES` applied to the session
model's entry only (every other value, and every other model, as captured),
written into the session's ``CODEX_HOME`` as :data:`CATALOG_NAME` (it goes with
the session's directory).

The repository's catalogs are ``codex_catalogs/codex-models-<version>.json``
(inside the package: the release installs only ``paw_backend``), the output of
``codex debug models`` of that version. ``0.156.1``'s was captured by the
Human with a ChatGPT login dedicated to the live check (the server's catalog;
no model was called), and the Human chose its values over the CLI's bundled
catalog (2026-11-16, Decision 0105, 2 A; ``deploy/cli-session/README.md``).
A CLI version gets its own file in the PR that verifies that version.

Everything fails closed (:class:`CatalogRefused`, a fixed code; the session
then starts nothing and sends nothing): a version without a catalog file, a
file that cannot be read or is not a catalog (``{"models": [...]}``, each
entry an object with a unique string ``slug``; no duplicate keys, no
``NaN``), a model that is not in it exactly once, and an entry that after
the override still has a Code Mode value (a key named ``code_mode*``, a string
value starting with ``code_mode``) or a multi-agent version (a key naming
``multi_agent`` whose value is not ``null``). The override only keeps Codex
from offering the extra tools: Decision 0101's tool check, the smoke's
``additional_tools`` failure and the item detection stay as they are.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

#: The repository's catalogs (see the module).
CATALOG_DIR: Final = Path(__file__).resolve().with_name("codex_catalogs")
#: The session's catalog file in its ``CODEX_HOME`` (``model_catalog_json``).
CATALOG_NAME: Final = "models.json"
#: The values written over the session model's entry (Decision 0104, 1 A,
#: the table): each one left as captured offered a tool outside Decision
#: 0101 in the recorder smoke of 2026-11-12 (``tool_mode``: ``exec`` and
#: ``wait``; ``multi_agent_version``: ``collaboration__*`` (v2),
#: ``multi_agent_v1__*`` (v1); ``experimental_supported_tools``:
#: ``request_user_input_async``, ``clock__curr_time``; ``use_responses_lite``:
#: the tools in an ``additional_tools`` item; ``supports_search_tool``:
#: ``tool_search``). Exactly these five: another one is a Human's decision.
OVERRIDES: Final[Mapping[str, object]] = MappingProxyType(
    {
        "tool_mode": "direct",
        "multi_agent_version": None,
        "experimental_supported_tools": (),
        "use_responses_lite": False,
        "supports_search_tool": False,
    }
)
#: A catalog file is a few hundred KiB (0.156.1's: 461 KiB).
MAX_CATALOG_BYTES: Final = 8 * 1024 * 1024
_VERSION: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*")


class CatalogRefused(Exception):
    """No catalog for the session (it starts nothing). ``code``: ``missing``
    (no file for the version), ``unreadable``, ``malformed``,
    ``model_not_in_catalog``, ``code_mode_left``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def catalog_path(version: str, directory: Path = CATALOG_DIR) -> Path:
    """The repository's catalog file of ``version`` (it may not exist)."""
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CatalogRefused("missing")
    return Path(directory) / f"codex-models-{version}.json"


def parse_catalog(data: bytes | str) -> dict:
    """A catalog file's content as a catalog, or :class:`CatalogRefused`
    (``malformed``)."""
    try:
        catalog = json.loads(
            data,
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
        )
    except (ValueError, RecursionError) as error:
        raise CatalogRefused("malformed") from error
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise CatalogRefused("malformed")
    slugs: set[str] = set()
    for entry in catalog["models"]:
        if not isinstance(entry, dict):
            raise CatalogRefused("malformed")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug or slug in slugs:
            raise CatalogRefused("malformed")
        slugs.add(slug)
    return catalog


def overridden(catalog: Mapping, model: str) -> dict:
    """``catalog`` (as :func:`parse_catalog` returns it) with
    :data:`OVERRIDES` applied to ``model``'s entry only; a new object (the
    given one is not changed). :class:`CatalogRefused` when ``model`` is not
    in it or its entry still has a Code Mode or multi-agent value."""
    entries = catalog["models"]
    matches = [i for i, entry in enumerate(entries) if entry.get("slug") == model]
    if len(matches) != 1:
        raise CatalogRefused("model_not_in_catalog")
    (index,) = matches
    entry = dict(entries[index])
    for key, value in OVERRIDES.items():
        entry[key] = list(value) if isinstance(value, tuple) else value
    check_entry(entry)
    models = list(entries)
    models[index] = entry
    return {**catalog, "models": models}


def check_entry(entry: Mapping) -> None:
    """Fail closed on what the override must have removed (see the module)."""
    for key, value in OVERRIDES.items():
        expected = list(value) if isinstance(value, tuple) else value
        if key not in entry or entry[key] != expected:
            raise CatalogRefused("code_mode_left")
    for key, value in entry.items():
        if key.startswith("code_mode"):
            raise CatalogRefused("code_mode_left")
        if isinstance(value, str) and value.startswith("code_mode"):
            raise CatalogRefused("code_mode_left")
        if "multi_agent" in key and value is not None:
            raise CatalogRefused("code_mode_left")


def render(catalog: Mapping) -> str:
    """The session's catalog file's text."""
    return json.dumps(catalog, ensure_ascii=True, separators=(",", ":")) + "\n"


class CodexCatalogs:
    """The catalogs of the CLI versions: the repository's files
    (``directory``, :data:`CATALOG_DIR` by default) or, for the tests, given
    ones (``catalogs``: ``{version: catalog}``, each checked as a file's).
    :meth:`session_catalog` is the text of a session's catalog file."""

    def __init__(
        self,
        directory: Path = CATALOG_DIR,
        *,
        catalogs: Mapping[str, object] | None = None,
    ) -> None:
        self._directory = Path(directory)
        self._given = None if catalogs is None else dict(catalogs)
        self._loaded: dict[str, dict] = {}
        self._rendered: dict[tuple[str, str], str] = {}

    def __repr__(self) -> str:
        return "CodexCatalogs()"

    def catalog(self, version: str) -> dict:
        """``version``'s catalog as captured (checked; read once)."""
        loaded = self._loaded.get(version)
        if loaded is not None:
            return loaded
        if self._given is not None:
            if version not in self._given:
                raise CatalogRefused("missing")
            loaded = parse_catalog(json.dumps(self._given[version]))
        else:
            loaded = parse_catalog(_read(catalog_path(version, self._directory)))
        self._loaded[version] = loaded
        return loaded

    def session_catalog(self, version: str, model: str) -> str:
        """The catalog file of a session of ``model`` on CLI ``version``, or
        :class:`CatalogRefused`."""
        key = (version, model)
        text = self._rendered.get(key)
        if text is None:
            text = render(overridden(self.catalog(version), model))
            self._rendered[key] = text
        return text


def _read(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_CATALOG_BYTES + 1)
    except FileNotFoundError as error:
        raise CatalogRefused("missing") from error
    except OSError as error:
        raise CatalogRefused("unreadable") from error
    if len(data) > MAX_CATALOG_BYTES:
        raise CatalogRefused("malformed")
    return data


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(name: str) -> object:
    raise ValueError(name)


#: The repository's catalogs, as production's adapters use them.
REPOSITORY_CATALOGS: Final = CodexCatalogs()

__all__ = [
    "CATALOG_DIR",
    "CATALOG_NAME",
    "OVERRIDES",
    "REPOSITORY_CATALOGS",
    "CatalogRefused",
    "CodexCatalogs",
    "catalog_path",
    "check_entry",
    "overridden",
    "parse_catalog",
    "render",
]
