"""The model catalogs of the fake ``codex``'s versions (Decision 0104, 2 A).

The repository has a catalog for each real CLI version it verified
(``paw_backend/agents/codex_catalogs``); the runtime and probe tests run a
fake ``codex`` of other versions (``0.162.1`` and others) and test models.
:func:`fake_catalogs` gives the adapters a catalog for those, whose entries
carry the Code Mode values of ``0.156.1``'s catalog models (so the override
is what a session gets).
"""

from paw_backend.agents.codex_catalog import CodexCatalogs

#: The test models' entries as ``0.156.1``'s ``gpt-6-sol`` has them (the five
#: values the override replaces, and a few it keeps).
CODE_MODE_ENTRY = {
    "display_name": "Test",
    "context_window": 272000,
    "base_instructions": "You are a test model.",
    "tool_mode": "code_mode_only",
    "multi_agent_version": "v2",
    "experimental_supported_tools": ["send_user_message_async", "clock"],
    "use_responses_lite": True,
    "supports_search_tool": True,
    "supports_experimental_context": True,
}
TEST_MODELS = ("gpt-5.5-codex", "gpt-test-codex", "gpt-6-sol", "gpt-5.5")
TEST_VERSIONS = ("0.162.1", "0.163.0", "0.170.0")


def fake_catalog(models=TEST_MODELS) -> dict:
    return {"models": [{"slug": slug, **CODE_MODE_ENTRY} for slug in models]}


def fake_catalogs(versions=TEST_VERSIONS, models=TEST_MODELS) -> CodexCatalogs:
    """Catalogs of ``versions`` with ``models`` in each (see the module)."""
    return CodexCatalogs(catalogs={v: fake_catalog(models) for v in versions})
