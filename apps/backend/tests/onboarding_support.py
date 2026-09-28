"""Fixtures of the PAW-024 service tests (invitations, pairing, lifecycle)."""

import uuid

from paw_backend.auth.context import RequestContext
from paw_backend.auth.sessions import AuthenticatedSession
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal

from .auth_support import PASSWORD, PostgresAuthTestCase, TestUser

__all__ = ["PASSWORD", "OnboardingTestCase"]


class OnboardingTestCase(PostgresAuthTestCase):
    """``PostgresAuthTestCase`` with signed-in administrators and step-ups."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.invitations = self.services.invitations
        self.pairing = self.services.pairing
        self.lifecycle = self.services.lifecycle

    def context(self, source: str = "198.51.100.1") -> RequestContext:
        return RequestContext(uuid.uuid4(), source)

    async def sign_in(self, user: TestUser, *, step_up: bool = True):
        """``user``'s session (and its Passkey step-up, written by the fixture)."""
        result = await self.auth.login(user.login_name, user.password, self.context())
        auth: AuthenticatedSession = result.session
        if step_up:
            await self.fake_passkey_step_up(auth.record.id)
        return auth

    def principal(self, user: TestUser) -> Principal:
        return Principal(user_id=user.id, system_role=SystemRole(user.role))

    async def administrator(self, role: str = "admin", name: str | None = None):
        """An administrator with a stepped-up session: ``(user, principal, auth)``."""
        user = await self.make_user(name or f"{role}-one", role=role)
        auth = await self.sign_in(user)
        return user, self.principal(user), auth

    async def status_of(self, user_id: uuid.UUID) -> str:
        return await self.scalar("SELECT status FROM users WHERE id = :id", id=user_id)

    async def history_of(self, user_id: uuid.UUID) -> list[tuple]:
        rows = await self.query(
            "SELECT old_status, new_status FROM user_status_changes "
            "WHERE user_id = :id ORDER BY recorded_at",
            id=user_id,
        )
        return [(row.old_status, row.new_status) for row in rows]

    async def everything_stored(self) -> str:
        parts = [await super().everything_stored()]
        for table in ("user_invitations", "device_pairings", "user_status_changes"):
            parts += [
                str(row[0])
                for row in await self.query(f"SELECT t::text FROM {table} t")
            ]
        return "\n".join(parts)
