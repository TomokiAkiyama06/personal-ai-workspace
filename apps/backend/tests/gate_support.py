"""The Project state gate for tests and tools that have no projects (issue #83).

``TaskService`` and ``TaskQueue`` REQUIRE a project gate (Decision 0020, approved
2026-09-26): there is no way to build one without, so a composition can never skip
the check by forgetting it. The task lane's own tests and the tools' tests create
tasks for made-up project ids; they pass :data:`ALWAYS_ACTIVE` to say so, in plain
words, at every construction.

This module is TEST SUPPORT. It is not part of ``paw_backend`` and must never be
imported or copied by production code: a service built with it admits work in any
project, Pending deletion and Archived ones included. ``test_project_state_gate``
(``LayeringTest``) fails if a name of it appears anywhere under ``paw_backend``.
"""

import uuid

from sqlalchemy import ColumnElement, true
from sqlalchemy.ext.asyncio import AsyncSession


class AlwaysActiveGate:
    """Every project is Active: no lock, no refusal, no filter."""

    async def require_active(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        return None

    def active_condition(self, project_id: ColumnElement) -> ColumnElement[bool]:
        return true()


ALWAYS_ACTIVE = AlwaysActiveGate()

__all__ = ["ALWAYS_ACTIVE", "AlwaysActiveGate"]
