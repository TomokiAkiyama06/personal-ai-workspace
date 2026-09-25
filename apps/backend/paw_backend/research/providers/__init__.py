"""Research provider adapter interface (PAW-051).

The Main Agent uses ``ResearchBroker`` with a ``ResearchRequest`` and receives a
``ResearchResult`` of items that share one ``SourceMetadata`` shape. Adapters
for Direct Web, Docs, GitHub and a future OpenCode implement
``ResearchProvider`` and are registered in a ``ProviderRegistry``. No concrete
adapter exists yet: they need credentials and a network policy.

``ResearchBroker.gather`` needs a privacy pre-flight (PAW-053): without one it
refuses to search unless the broker was built with ``unfiltered=True``.
"""

from paw_backend.research.providers.broker import ResearchBroker, classify_failure
from paw_backend.research.providers.contract import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    DEFAULT_TIME_BUDGET_SECONDS,
    KIND_ORDER,
    MAX_DOCUMENT_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_LOCATOR_CHARS,
    MAX_PROVIDER_TIMEOUT_SECONDS,
    MAX_PROVIDERS,
    MAX_QUERY_CHARS,
    MAX_RESULTS_LIMIT,
    MAX_TIME_BUDGET_SECONDS,
    MAX_TITLE_CHARS,
    ProviderDocument,
    ProviderHit,
    ProviderKind,
    ResearchError,
    ResearchItem,
    ResearchProvider,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
    SourceType,
)
from paw_backend.research.providers.errors import (
    DuplicateProviderError,
    InvalidLocatorError,
    InvalidProviderResponseError,
    ProviderFailure,
    ProviderInterfaceError,
    ProviderRegistryError,
    RegistryFullError,
    ResearchErrorCode,
    UnknownProviderError,
)
from paw_backend.research.providers.locator import canonicalize_locator
from paw_backend.research.providers.normalize import (
    compute_content_hash,
    merge_items,
    normalize_hits,
    normalize_title,
)
from paw_backend.research.providers.preflight import (
    PreflightNotConfiguredError,
    PreflightRequiredError,
    SearchPreflight,
    validate_preflight,
)
from paw_backend.research.providers.registry import (
    ProviderRegistry,
    RegisteredProvider,
    validate_provider,
)
from paw_backend.research.providers.static import StaticProvider

__all__ = [
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "DEFAULT_TIME_BUDGET_SECONDS",
    "KIND_ORDER",
    "MAX_DOCUMENT_CHARS",
    "MAX_EXCERPT_CHARS",
    "MAX_LOCATOR_CHARS",
    "MAX_PROVIDERS",
    "MAX_PROVIDER_TIMEOUT_SECONDS",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS_LIMIT",
    "MAX_TIME_BUDGET_SECONDS",
    "MAX_TITLE_CHARS",
    "DuplicateProviderError",
    "InvalidLocatorError",
    "InvalidProviderResponseError",
    "ProviderDocument",
    "ProviderFailure",
    "ProviderHit",
    "ProviderInterfaceError",
    "ProviderKind",
    "PreflightNotConfiguredError",
    "PreflightRequiredError",
    "ProviderRegistry",
    "ProviderRegistryError",
    "RegisteredProvider",
    "RegistryFullError",
    "ResearchBroker",
    "ResearchError",
    "ResearchErrorCode",
    "ResearchItem",
    "ResearchProvider",
    "ResearchRequest",
    "ResearchResult",
    "SearchPreflight",
    "SourceMetadata",
    "SourceType",
    "StaticProvider",
    "UnknownProviderError",
    "canonicalize_locator",
    "classify_failure",
    "compute_content_hash",
    "merge_items",
    "normalize_hits",
    "normalize_title",
    "validate_preflight",
    "validate_provider",
]
