"""The production construction path of the Research Privacy Filter (issue #87).

``build_privacy_gate`` and ``build_research_broker`` are the ways to get a gate and a
broker whose sink is the persistent one (``PostgresExternalSendAudit``, the
append-only ``audit_events`` table). Use them wherever the backend builds a
``ResearchBroker`` for real; ``PrivacyGate(InMemoryExternalSendAudit())`` stays for
tests. Decision 0010's condition for feeding private-derived context into research
(a persistent audit sink is connected) is met by building the broker here: a send
whose record cannot be stored in time is refused (fail closed).

The gate and the sink get the SAME deadline (``audit_timeout_seconds``, default the
gate's own ``DEFAULT_AUDIT_TIMEOUT_SECONDS``, 5 seconds). The gate's timeout cancels
the write, which aborts the database connection; the sink's own deadline (which
also covers the wait for a free connection slot) ends it at the same time if the
cancellation is late.
"""

from collections.abc import Callable
from datetime import datetime

from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.research.privacy.audit import PostgresExternalSendAudit
from paw_backend.research.privacy.contract import DEFAULT_AUDIT_TIMEOUT_SECONDS
from paw_backend.research.privacy.gate import PrivacyGate
from paw_backend.research.providers.broker import ResearchBroker
from paw_backend.research.providers.registry import ProviderRegistry

__all__ = ["build_privacy_gate", "build_research_broker"]


def build_privacy_gate(
    database: Database,
    *,
    audit_timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> PrivacyGate:
    """A ``PrivacyGate`` that records every authorised send in ``audit_events``.

    ``database`` must be a ``Database`` (``TypeError``) with a configured URL
    (``DatabaseNotConfiguredError``: a gate that could never record would refuse
    every send, so it is not built at all). ``audit_timeout_seconds`` is checked by
    the sink and the gate (``TypeError`` / ``ValueError``); ``clock`` is the gate's
    clock (see ``PrivacyGate``). Nothing connects to the database here.
    """
    if not isinstance(database, Database):
        raise TypeError("database must be a Database")
    if not database.configured:
        raise DatabaseNotConfiguredError("PAW_DATABASE_URL is not set")
    sink = PostgresExternalSendAudit(database, timeout_seconds=audit_timeout_seconds)
    return PrivacyGate(sink, clock=clock, audit_timeout_seconds=audit_timeout_seconds)


def build_research_broker(
    registry: ProviderRegistry,
    database: Database,
    *,
    audit_timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> ResearchBroker:
    """A ``ResearchBroker`` whose pre-flight is ``build_privacy_gate(database)``.

    ``registry`` must be a ``ProviderRegistry`` (``TypeError``). ``clock`` is the
    clock of both the gate and the broker. The broker is never ``unfiltered``.
    """
    if not isinstance(registry, ProviderRegistry):
        raise TypeError("registry must be a ProviderRegistry")
    gate = build_privacy_gate(
        database, audit_timeout_seconds=audit_timeout_seconds, clock=clock
    )
    return ResearchBroker(registry, clock=clock, preflight=gate)
