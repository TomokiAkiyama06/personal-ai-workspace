"""Issuing the one-time password reset token of an Admin or a User (#108).

REQUIREMENTS.md: an Owner / Admin never sees another user's password; when one is
needed, a one-time password reset flow is issued. The token is the same kind as the
Owner's setup / recovery tokens (``paw_backend.identity.tokens``: ``pawst1.<id>.
<secret>``, only a salted HMAC stored, spent with ``POST /auth/token/redeem``), with
the purpose ``password_reset``.

The web application's database role has no INSERT on ``setup_tokens`` (whoever can
insert a token row can take the Owner's account, Decision 0005). The row is written
by ``paw_issue_password_reset_token`` (revision ``0108``, ``SECURITY DEFINER``),
which refuses the Owner and anyone who is not ``invited`` / ``active``, revokes the
user's outstanding token and deletes the user's password in the same statement
sequence. So even a compromised application cannot mint an Owner token this way.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.identity import tokens as identity_tokens

# Bounds of ``Settings.password_reset_token_ttl_seconds``; the database function
# refuses anything longer than 72 hours as well.
MIN_RESET_TOKEN_TTL_SECONDS = 600
MAX_RESET_TOKEN_TTL_SECONDS = 259_200
DEFAULT_RESET_TOKEN_TTL_SECONDS = 86_400


class ResetTokenRefused(Exception):
    """The database refused to issue the token (the user is not eligible)."""


@dataclass(frozen=True, slots=True)
class IssuedResetToken:
    """A freshly issued token. ``token`` is shown once to the actor, never stored."""

    token: str = field(repr=False)
    # What the audit trail calls the token (never its lookup id).
    audit_ref: uuid.UUID
    expires_at: datetime


def validate_ttl(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_RESET_TOKEN_TTL_SECONDS <= value <= MAX_RESET_TOKEN_TTL_SECONDS
    ):
        raise ValueError(
            "reset_token_ttl_seconds must be an integer from "
            f"{MIN_RESET_TOKEN_TTL_SECONDS} to {MAX_RESET_TOKEN_TTL_SECONDS}"
        )
    return value


async def issue_in(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime,
    ttl_seconds: int,
) -> IssuedResetToken:
    """Issue the user's reset token in the caller's transaction (see the module).

    The caller holds the user's row ``FOR UPDATE`` already (the function takes the
    same lock again, a no-op then). Raises ``ResetTokenRefused`` if the function
    refuses; the caller rolls back.
    """
    if not isinstance(session, AsyncSession):
        raise InvalidAuthInputError("session")
    if not isinstance(user_id, uuid.UUID):
        raise InvalidAuthInputError("user_id")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise InvalidAuthInputError("now")
    ttl_seconds = validate_ttl(ttl_seconds)
    new = identity_tokens.generate()
    expires_at = now + timedelta(seconds=ttl_seconds)
    issued = (
        await session.execute(
            text(
                "SELECT paw_issue_password_reset_token(:user_id, :token_id, "
                ":audit_ref, :salt, :secret_hash, :created_at, :expires_at)"
            ),
            {
                "user_id": user_id,
                "token_id": new.token_id,
                "audit_ref": new.audit_ref,
                "salt": new.salt,
                "secret_hash": new.secret_hash,
                "created_at": now,
                "expires_at": expires_at,
            },
        )
    ).scalar_one()
    if issued is not True:
        raise ResetTokenRefused
    return IssuedResetToken(new.token, new.audit_ref, expires_at)
