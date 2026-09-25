"""The Owner-controlled authentication policy (Decision 0015).

REQUIREMENTS.md "Passkey Policy" fixes a policy: Owner and Admin need a Passkey,
a User may use the password alone but is urged to register a Passkey, and the
Owner's and Admin's sensitive operations need a Step-up within the last 30
minutes. Decision 0015 turns that fixed text into the **default** of a workspace
setting that only the Owner can change. The record (``auth_policy``) holds the
requirement per role, whether Users are urged, and the Step-up window; the
migration seeds the requirements' values.

* **Who.** ``update`` needs the Owner (the route's capability
  ``owner.auth_policy.manage`` is Owner-only and never delegable to an Agent;
  the service checks the role again). ``get`` is for an Admin or the Owner.
* **Optimistic lock.** ``update`` takes the version the caller read. Under a row
  lock a different version is refused (``PolicyVersionConflictError``), so two
  concurrent edits cannot lose an update; every change moves the version up by
  exactly one (also a trigger of the migration).
* **Step-up.** The change needs a step-up of the Owner's own session within the
  policy's window (password re-entry until PAW-023 adds Passkeys): a stolen
  session cannot relax the policy. The window is judged by the database's clock
  after the row lock is held.
* **History.** Every change writes ``auth_policy_changes`` (who, when, each field
  before and after) and an audit event, in the same transaction.
* **Effect.** The policy applies to NEW sign-ins and new sessions. Changing it
  never touches an existing session (tightening does not silently sign anyone
  out); a client learns the current requirement from the session response.
* **No lock-out.** Nothing in this issue enforces a Passkey, so the Owner can
  always sign in with the password whatever is set. PAW-023 must keep that true:
  "required" and not enrolled is an enrollment-only state, never a dead end, and
  ``owner-recover`` stays.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run
from paw_backend.auth.errors import (
    AuthPermissionError,
    InvalidAuthInputError,
    PolicyVersionConflictError,
    StepUpRequiredError,
)
from paw_backend.auth.models import PasskeyRequirement
from paw_backend.auth.state import AuthPolicy
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.db import Database

MIN_STEPUP_WINDOW_MINUTES = 5
MAX_STEPUP_WINDOW_MINUTES = 240
MAX_VERSION = 2_147_483_646

_SELECT = """
SELECT version, passkey_owner, passkey_admin, passkey_user,
       recommend_passkey_to_users, stepup_window_minutes, updated_at, updated_by
  FROM auth_policy WHERE id = 1
"""


def _policy(row) -> AuthPolicy:
    return AuthPolicy(
        version=row.version,
        passkey_owner=PasskeyRequirement(row.passkey_owner),
        passkey_admin=PasskeyRequirement(row.passkey_admin),
        passkey_user=PasskeyRequirement(row.passkey_user),
        recommend_passkey_to_users=row.recommend_passkey_to_users,
        stepup_window_minutes=row.stepup_window_minutes,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _requirement(name: str, value: object) -> PasskeyRequirement:
    if isinstance(value, PasskeyRequirement):
        return value
    if isinstance(value, str):
        try:
            return PasskeyRequirement(value)
        except ValueError:
            pass
    raise InvalidAuthInputError(name)


def _strict_int(name: str, value: object, low: int, high: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise InvalidAuthInputError(name)
    return value


class AuthPolicyService:
    """Reads and changes the authentication policy. See the module docstring."""

    def __init__(
        self, database: Database, audit: AuthAudit, *, timeout_seconds: float
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(audit, AuthAudit):
            raise TypeError("audit must be an AuthAudit")
        self._database = database
        self._audit = audit
        self._timeout = float(timeout_seconds)

    async def get(self) -> AuthPolicy:
        """The current policy."""

        async def work(session: AsyncSession) -> AuthPolicy:
            return await self.get_in(session)

        return await run(self._database, work, self._timeout)

    @staticmethod
    async def get_in(session: AsyncSession) -> AuthPolicy:
        """The current policy, read in the caller's transaction."""
        return _policy((await session.execute(text(_SELECT))).one())

    async def update(
        self,
        actor: Principal,
        session_id: uuid.UUID,
        context: RequestContext,
        *,
        expected_version: int,
        passkey_owner: PasskeyRequirement | str,
        passkey_admin: PasskeyRequirement | str,
        passkey_user: PasskeyRequirement | str,
        recommend_passkey_to_users: bool,
        stepup_window_minutes: int,
    ) -> AuthPolicy:
        """Replace the policy (the Owner, after a step-up, from ``expected_version``).

        Every argument is checked before the database is touched. Setting the
        values it already has changes nothing (no new version, no event).
        """
        if not isinstance(actor, Principal):
            raise InvalidAuthInputError("actor")
        if not isinstance(session_id, uuid.UUID):
            raise InvalidAuthInputError("session_id")
        if not isinstance(context, RequestContext):
            raise InvalidAuthInputError("context")
        expected_version = _strict_int(
            "expected_version", expected_version, 1, MAX_VERSION
        )
        new = {
            "passkey_owner": _requirement("passkey_owner", passkey_owner),
            "passkey_admin": _requirement("passkey_admin", passkey_admin),
            "passkey_user": _requirement("passkey_user", passkey_user),
            "recommend_passkey_to_users": recommend_passkey_to_users,
            "stepup_window_minutes": _strict_int(
                "stepup_window_minutes",
                stepup_window_minutes,
                MIN_STEPUP_WINDOW_MINUTES,
                MAX_STEPUP_WINDOW_MINUTES,
            ),
        }
        if not isinstance(recommend_passkey_to_users, bool):
            raise InvalidAuthInputError("recommend_passkey_to_users")

        if actor.system_role is not SystemRole.OWNER:
            await self._deny(actor, context, AuthReason.ROLE_NOT_ALLOWED)
            raise AuthPermissionError

        denial: AuthReason | None = None

        async def work(session: AsyncSession) -> AuthPolicy | None:
            nonlocal denial
            row = (await session.execute(text(_SELECT + " FOR UPDATE"))).one()
            current = _policy(row)
            if current.version != expected_version:
                denial = AuthReason.VERSION_CONFLICT
                raise PolicyVersionConflictError
            # The window is judged now, under the row lock, by the later of the
            # service clock and the database's: a step-up that is older than the
            # current window is no step-up.
            fresh = (
                await session.execute(
                    text(
                        """
                        WITH clock AS (SELECT greatest(CAST(:now AS timestamptz),
                                                       clock_timestamp()) AS ts)
                        SELECT EXISTS (
                            SELECT 1 FROM clock, auth_sessions s
                             WHERE s.id = :id AND s.user_id = :user_id
                               AND s.revoked_at IS NULL
                               AND s.idle_expires_at > clock.ts
                               AND s.absolute_expires_at > clock.ts
                               AND s.stepup_at IS NOT NULL
                               AND s.stepup_at + :window * interval '1 minute'
                                   > clock.ts)
                        """
                    ),
                    {
                        "now": self._audit.now(),
                        "id": session_id,
                        "user_id": actor.user_id,
                        "window": current.stepup_window_minutes,
                    },
                )
            ).scalar_one()
            if not fresh:
                denial = AuthReason.STEP_UP_REQUIRED
                raise StepUpRequiredError
            if all(getattr(current, field) == value for field, value in new.items()):
                return current
            now = self._audit.now()
            change_id = uuid.uuid4()
            version = current.version + 1
            await session.execute(
                text(
                    """
                    UPDATE auth_policy SET version = :version,
                        passkey_owner = :passkey_owner, passkey_admin = :passkey_admin,
                        passkey_user = :passkey_user,
                        recommend_passkey_to_users = :recommend,
                        stepup_window_minutes = :window,
                        updated_at = :now, updated_by = :actor
                     WHERE id = 1
                    """
                ),
                {
                    "version": version,
                    "passkey_owner": new["passkey_owner"].value,
                    "passkey_admin": new["passkey_admin"].value,
                    "passkey_user": new["passkey_user"].value,
                    "recommend": new["recommend_passkey_to_users"],
                    "window": new["stepup_window_minutes"],
                    "now": now,
                    "actor": actor.user_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO auth_policy_changes (id, version, changed_by,
                        changed_at,
                        old_passkey_owner, new_passkey_owner, old_passkey_admin,
                        new_passkey_admin, old_passkey_user, new_passkey_user,
                        old_recommend_passkey_to_users, new_recommend_passkey_to_users,
                        old_stepup_window_minutes, new_stepup_window_minutes)
                    VALUES (:id, :version, :actor, :now, :o_owner, :n_owner, :o_admin,
                        :n_admin, :o_user, :n_user, :o_rec, :n_rec, :o_win, :n_win)
                    """
                ),
                {
                    "id": change_id,
                    "version": version,
                    "actor": actor.user_id,
                    "now": now,
                    "o_owner": current.passkey_owner.value,
                    "n_owner": new["passkey_owner"].value,
                    "o_admin": current.passkey_admin.value,
                    "n_admin": new["passkey_admin"].value,
                    "o_user": current.passkey_user.value,
                    "n_user": new["passkey_user"].value,
                    "o_rec": current.recommend_passkey_to_users,
                    "n_rec": new["recommend_passkey_to_users"],
                    "o_win": current.stepup_window_minutes,
                    "n_win": new["stepup_window_minutes"],
                },
            )
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.POLICY_UPDATE,
                    AuthReason.UPDATED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=actor.user_id,
                    actor_role=actor.system_role,
                    resource_kind="auth_policy_change",
                    resource_id=change_id,
                ),
            )
            return await self.get_in(session)

        try:
            return await run(self._database, work, self._timeout)
        except (PolicyVersionConflictError, StepUpRequiredError):
            # Refused: the denial is recorded on its own (the transaction rolled back).
            if denial is not None:
                await self._deny(actor, context, denial)
            raise

    async def _deny(
        self, actor: Principal, context: RequestContext, reason: AuthReason
    ) -> None:
        await self._audit.record_best_effort(
            self._audit.event(
                AuthAction.POLICY_UPDATE,
                reason,
                allowed=False,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=actor.user_id,
                actor_role=actor.system_role,
                resource_kind="auth_policy",
            )
        )
