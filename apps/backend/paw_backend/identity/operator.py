"""The Owner's initial setup and recovery: the operator-only part.

There is exactly one way to create the Owner: ``OwnerOperator.setup_owner``,
called by the server-local command (``python -m paw_backend.cli``). Nothing on
the web can create an Owner, so the first visitor of the site is nobody.
``recover_owner`` issues a recovery token for the existing Owner. The web flow
(PAW-022) then spends the token with ``TokenRedeemer`` (``redeemer.py``).

**Only ``paw_backend.cli`` and ``paw_backend.identity`` may import this module**;
``tests/test_owner_no_web_path.py`` checks it (transitively) so that a web route
cannot call it through a helper. It needs the database role of the operator, not
of the web application (migration ``0021``, ``PAW_OPERATOR_DATABASE_URL``).

Every step is audited through the ``AuditSink`` (ids and enum values only, a
token named by its ``audit_ref``) and **fails closed**: the audit event is
written before the database transaction commits, so if it cannot be stored
nothing changes and no token is ever shown. (If the commit itself fails after
the event was stored, the trail holds an event for something that did not
happen; the token was never shown.)

Token handling: see ``paw_backend.identity.tokens``. The plaintext exists only in
the returned ``IssuedToken``; the database keeps a salted HMAC.
"""

import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.audit import AuditSink
from paw_backend.authz.roles import SystemRole
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.identity import tokens
from paw_backend.identity.audit import (
    AuditAction,
    AuditReason,
    IdentityAudit,
    require_int,
)
from paw_backend.identity.errors import (
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    OwnerNotLiveError,
)
from paw_backend.identity.limits import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
)
from paw_backend.identity.login_name import normalize_login_name
from paw_backend.identity.models import (
    MAX_UID,
    SetupTokenRow,
    TokenPurpose,
    UserRow,
    UserStatus,
    passkey_required_for,
)
from paw_backend.identity.redeemer import LIVE_STATUSES

# Constraint names the operator tells apart when an INSERT is refused.
_SINGLE_OWNER = "uq_users_single_owner"
_LOGIN_NAME_UNIQUE = "uq_users_login_name"


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """Who ran the management command: numeric ids only.

    ``sudo_uid`` is ``SUDO_UID``, which sudo sets. It is an environment variable,
    so it is a hint for the investigator, not proof of who the person was.
    """

    uid: int
    sudo_uid: int | None = None

    def __post_init__(self) -> None:
        require_int("uid", self.uid, 0, MAX_UID)
        if self.sudo_uid is not None:
            require_int("sudo_uid", self.sudo_uid, 0, MAX_UID)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "OperatorIdentity | None":
        """This process's uid and ``SUDO_UID``; ``None`` where there is no uid."""
        environ = os.environ if environ is None else environ
        try:
            uid = os.geteuid()
        except AttributeError:  # not a POSIX system
            return None
        raw = environ.get("SUDO_UID", "")
        sudo_uid = (
            int(raw)
            if raw.isascii()
            and raw.isdigit()
            and len(raw) <= 10
            and int(raw) <= MAX_UID
            else None
        )
        return cls(uid, sudo_uid)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A token to show to the operator once. ``token`` is not part of ``repr``."""

    token: str = field(repr=False)
    # What audit events name the token; not the lookup id inside ``token``.
    audit_ref: uuid.UUID
    user_id: uuid.UUID
    login_name: str
    purpose: TokenPurpose
    expires_at: datetime


class OwnerOperator:
    """Creates the Owner and issues setup / recovery tokens.

    It performs no authorisation of its own: the only caller is the server-local
    command, and whoever can run it has the operator's database credentials.
    """

    def __init__(
        self,
        database: Database,
        audit: AuditSink,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        audit_timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        require_int("ttl_seconds", ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS)
        self._database = database
        self._audit = IdentityAudit(
            audit, timeout_seconds=audit_timeout_seconds, clock=clock
        )
        self._ttl = timedelta(seconds=ttl_seconds)

    @classmethod
    def from_settings(
        cls, settings: Settings, database: Database, audit: AuditSink
    ) -> "OwnerOperator":
        return cls(
            database,
            audit,
            ttl_seconds=settings.setup_token_ttl_seconds,
            audit_timeout_seconds=settings.database_timeout_seconds,
        )

    # -- initial setup ------------------------------------------------------

    async def setup_owner(
        self,
        login_name: str,
        *,
        replace_non_live_owner: bool = False,
        operator: OperatorIdentity | None = None,
    ) -> IssuedToken:
        """Create the Owner and a one-time setup token for them.

        Refuses (``OwnerAlreadyExistsError``) when a live Owner exists. Two
        concurrent calls cannot both succeed: the database allows one Owner row
        (a unique index), so the second INSERT fails and is refused.

        An Owner row that is pending deletion or deleted is not live and
        cannot be recovered. It is refused with ``OwnerNotLiveError`` unless
        ``replace_non_live_owner`` says to replace it: the old account is then
        demoted to a plain ``user`` (its status and data untouched), its tokens
        are revoked, and the new Owner is created, all in one audited
        transaction. A live Owner is never replaced.
        """
        if not isinstance(replace_non_live_owner, bool):
            raise TypeError("replace_non_live_owner must be a bool")
        name = normalize_login_name(login_name)
        correlation_id = uuid.uuid4()
        try:
            return await self._create_owner(
                name, replace_non_live_owner, operator, correlation_id
            )
        except OwnerAlreadyExistsError:
            await self._refused(
                AuditAction.OWNER_CREATE, AuditReason.OWNER_EXISTS, correlation_id
            )
            raise
        except OwnerNotLiveError:
            await self._refused(
                AuditAction.OWNER_CREATE, AuditReason.OWNER_NOT_LIVE, correlation_id
            )
            raise
        except LoginNameTakenError:
            await self._refused(
                AuditAction.OWNER_CREATE, AuditReason.LOGIN_NAME_TAKEN, correlation_id
            )
            raise

    async def _create_owner(
        self,
        name: str,
        replace: bool,
        operator: OperatorIdentity | None,
        correlation_id: uuid.UUID,
    ) -> IssuedToken:
        now = self._audit.now()
        user_id = uuid.uuid4()
        new = tokens.generate()
        try:
            async with self._database.session() as session:
                old_owner = await _lock_owner(session)
                replaced_events = []
                if old_owner is not None:
                    if old_owner.status in LIVE_STATUSES:
                        raise OwnerAlreadyExistsError
                    if not replace:
                        raise OwnerNotLiveError(old_owner.status)
                    revoked = await _revoke_outstanding(session, old_owner.id, now)
                    old_owner.system_role = SystemRole.USER.value
                    old_owner.updated_at = now
                    await session.flush()
                    replaced_events = [
                        self._audit.event(
                            AuditAction.OWNER_REPLACE,
                            AuditReason.REPLACED,
                            correlation_id=correlation_id,
                            resource_id=old_owner.id,
                            old_role=SystemRole.OWNER,
                            new_role=SystemRole.USER,
                        ),
                        *(
                            self._audit.token_event(
                                AuditAction.TOKEN_REVOKE,
                                AuditReason.SUPERSEDED,
                                correlation_id,
                                audit_ref,
                            )
                            for audit_ref in revoked
                        ),
                    ]
                session.add(
                    UserRow(
                        id=user_id,
                        login_name=name,
                        system_role=SystemRole.OWNER.value,
                        status=UserStatus.INVITED.value,
                        passkey_required=passkey_required_for(SystemRole.OWNER),
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(
                    self._token_row(new, user_id, TokenPurpose.SETUP, now, operator)
                )
                await session.flush()
                for event in replaced_events:
                    await self._audit.record(event)
                await self._audit.record(
                    self._audit.event(
                        AuditAction.OWNER_CREATE,
                        AuditReason.CREATED,
                        correlation_id=correlation_id,
                        resource_id=user_id,
                        new_role=SystemRole.OWNER,
                    )
                )
                await self._audit.record(
                    self._audit.token_event(
                        AuditAction.SETUP_TOKEN_ISSUE,
                        AuditReason.ISSUED,
                        correlation_id,
                        new.audit_ref,
                    )
                )
                await session.commit()
        except IntegrityError as error:
            constraint = _violated_constraint(error)
            if constraint == _SINGLE_OWNER:
                raise OwnerAlreadyExistsError from None
            if constraint == _LOGIN_NAME_UNIQUE:
                raise LoginNameTakenError from None
            raise
        return IssuedToken(
            token=new.token,
            audit_ref=new.audit_ref,
            user_id=user_id,
            login_name=name,
            purpose=TokenPurpose.SETUP,
            expires_at=now + self._ttl,
        )

    # -- recovery -----------------------------------------------------------

    async def recover_owner(
        self, *, operator: OperatorIdentity | None = None
    ) -> IssuedToken:
        """Issue a recovery token for the existing Owner.

        Every outstanding token of the Owner is revoked first (each is audited),
        so only the new one works. Refuses with ``OwnerNotFoundError`` when
        there is no Owner and with ``OwnerNotLiveError`` when the Owner account
        is pending deletion or deleted (``setup_owner`` can replace it).
        """
        correlation_id = uuid.uuid4()
        try:
            return await self._issue_recovery(operator, correlation_id)
        except OwnerNotFoundError:
            await self._refused(
                AuditAction.RECOVERY_TOKEN_ISSUE,
                AuditReason.OWNER_MISSING,
                correlation_id,
            )
            raise
        except OwnerNotLiveError:
            await self._refused(
                AuditAction.RECOVERY_TOKEN_ISSUE,
                AuditReason.OWNER_NOT_LIVE,
                correlation_id,
            )
            raise

    async def _issue_recovery(
        self, operator: OperatorIdentity | None, correlation_id: uuid.UUID
    ) -> IssuedToken:
        now = self._audit.now()
        new = tokens.generate()
        async with self._database.session() as session:
            # The row lock serialises concurrent recoveries (and a redemption,
            # which takes the same lock first): revoke-then-insert cannot
            # interleave with another one.
            owner = await _lock_owner(session)
            if owner is None:
                raise OwnerNotFoundError
            if owner.status not in LIVE_STATUSES:
                raise OwnerNotLiveError(owner.status)
            revoked = await _revoke_outstanding(session, owner.id, now)
            session.add(
                self._token_row(new, owner.id, TokenPurpose.RECOVERY, now, operator)
            )
            await session.flush()
            for audit_ref in revoked:
                await self._audit.record(
                    self._audit.token_event(
                        AuditAction.TOKEN_REVOKE,
                        AuditReason.SUPERSEDED,
                        correlation_id,
                        audit_ref,
                    )
                )
            await self._audit.record(
                self._audit.token_event(
                    AuditAction.RECOVERY_TOKEN_ISSUE,
                    AuditReason.ISSUED,
                    correlation_id,
                    new.audit_ref,
                )
            )
            await session.commit()
            login_name = owner.login_name
            user_id = owner.id
        return IssuedToken(
            token=new.token,
            audit_ref=new.audit_ref,
            user_id=user_id,
            login_name=login_name,
            purpose=TokenPurpose.RECOVERY,
            expires_at=now + self._ttl,
        )

    # -- helpers ------------------------------------------------------------

    def _token_row(
        self,
        new: tokens.NewToken,
        user_id: uuid.UUID,
        purpose: TokenPurpose,
        now: datetime,
        operator: OperatorIdentity | None,
    ) -> SetupTokenRow:
        return SetupTokenRow(
            id=new.token_id,
            audit_ref=new.audit_ref,
            user_id=user_id,
            purpose=purpose.value,
            salt=new.salt,
            secret_hash=new.secret_hash,
            created_at=now,
            expires_at=now + self._ttl,
            attempts=0,
            issued_by_uid=operator.uid if operator is not None else None,
            issued_by_sudo_uid=operator.sudo_uid if operator is not None else None,
        )

    async def _refused(
        self, action: AuditAction, reason: AuditReason, correlation_id: uuid.UUID
    ) -> None:
        """Audit a refusal (best effort: it stands even if this cannot be stored)."""
        await self._audit.record_best_effort(
            self._audit.event(
                action, reason, correlation_id=correlation_id, allowed=False
            )
        )


async def _lock_owner(session: AsyncSession) -> UserRow | None:
    """The Owner row, locked, whatever its status; ``None`` if there is none."""
    return (
        await session.execute(
            select(UserRow)
            .where(UserRow.system_role == SystemRole.OWNER.value)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _revoke_outstanding(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> list[uuid.UUID]:
    """Revoke the user's unused, unrevoked tokens; their ``audit_ref``s."""
    result = await session.execute(
        update(SetupTokenRow)
        .where(
            SetupTokenRow.user_id == user_id,
            SetupTokenRow.used_at.is_(None),
            SetupTokenRow.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(SetupTokenRow.audit_ref)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars())


def _violated_constraint(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None
