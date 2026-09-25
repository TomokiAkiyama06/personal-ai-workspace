"""Building the authentication services and installing them on the application."""

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI

from paw_backend.auth.audit import AuthAudit
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys.approvals import PasskeyApprovalStepUp
from paw_backend.auth.passkeys.config import PasskeyConfig
from paw_backend.auth.passkeys.service import PasskeyService, PasskeyStepUpVerifier
from paw_backend.auth.passkeys.store import PasskeyRegistry
from paw_backend.auth.passwords import PasswordHasher
from paw_backend.auth.principals import (
    DatabasePrincipalDirectory,
    SessionPrincipalProvider,
)
from paw_backend.auth.service import AuthService
from paw_backend.auth.sessions import SessionLifetimes, SessionStore
from paw_backend.auth.throttle import Throttle, policies_from_settings
from paw_backend.authz import install_authz
from paw_backend.authz.audit import AuditSink, PostgresAuditSink
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.identity import TokenRedeemer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthServices:
    """Everything the authentication endpoints and the request guard use."""

    service: AuthService
    sessions: SessionStore
    throttle: Throttle
    policy: AuthPolicyService
    provider: SessionPrincipalProvider
    directory: DatabasePrincipalDirectory
    hasher: PasswordHasher
    audit_sink: AuditSink
    samesite: str
    passkeys: PasskeyService
    # The Tool Broker's strong-approval ``StepUpVerifier``, answered by the Passkey
    # Step-up: pass it as ``ApprovalService(step_up=...)``. Not installed anywhere by
    # itself: ``ApprovalService`` keeps its fail-closed default until a deployment
    # (the approval endpoint) opts in.
    approval_step_up: PasskeyApprovalStepUp

    async def start(self) -> None:
        """Make the dummy hash now, so that the first unknown login is not slower."""
        with contextlib.suppress(Exception):
            await self.hasher.warm()
        if not self.passkeys.available:
            # Loud, once: the requirement is not enforced without this.
            logger.warning(
                "Passkeys are not configured (PAW_PASSKEY_RP_ID, PAW_PASSKEY_ORIGINS): "
                "the Owner / Admin Passkey requirement is NOT enforced and Passkey "
                "step-up is unavailable, so the sensitive operations that need it "
                "(policy change, unlocking an account) are refused."
            )

    def close(self) -> None:
        self.hasher.close()


def build_auth(
    settings: Settings,
    database: Database,
    *,
    audit_sink: AuditSink | None = None,
    clock=None,
) -> AuthServices:
    """Build the services from ``settings`` (``clock`` is the seam of the tests)."""
    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    if not isinstance(database, Database):
        raise TypeError("database must be a Database")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")
    clock = clock or _utc_now
    sink = audit_sink or PostgresAuditSink(database)
    timeout = settings.database_timeout_seconds
    audit = AuthAudit(sink, timeout_seconds=timeout, clock=clock)
    hasher = PasswordHasher.from_settings(settings)
    sessions = SessionStore(SessionLifetimes.from_settings(settings), clock=clock)
    throttle = Throttle(
        database,
        policies_from_settings(settings),
        clock=clock,
        timeout_seconds=timeout,
    )
    policy = AuthPolicyService(database, audit, timeout_seconds=timeout)
    registry = PasskeyRegistry(
        database,
        PasskeyConfig.from_settings(settings),
        clock=clock,
        timeout_seconds=timeout,
    )
    verifiers = (
        {
            AuthMethod.PASSKEY: PasskeyStepUpVerifier(
                database, registry, timeout_seconds=timeout
            )
        }
        if registry.available
        else {}
    )
    redeemer = TokenRedeemer(
        database,
        sink,
        max_attempts=settings.setup_token_max_attempts,
        audit_timeout_seconds=timeout,
        clock=clock,
    )
    service = AuthService(
        database,
        hasher=hasher,
        sessions=sessions,
        throttle=throttle,
        audit=audit,
        policy=policy,
        redeemer=redeemer,
        passkeys=registry,
        step_up_verifiers=verifiers,
        # Owner Recovery ends every Passkey with the token's transaction.
        credential_invalidators=(registry.revoke_all_in,),
        timeout_seconds=timeout,
    )
    return AuthServices(
        service=service,
        sessions=sessions,
        throttle=throttle,
        policy=policy,
        provider=SessionPrincipalProvider(database, sessions, timeout_seconds=timeout),
        directory=DatabasePrincipalDirectory(database, timeout_seconds=timeout),
        hasher=hasher,
        audit_sink=sink,
        samesite=settings.session_cookie_samesite,
        passkeys=PasskeyService(
            database,
            registry,
            sessions,
            throttle,
            audit,
            policy,
            timeout_seconds=timeout,
        ),
        approval_step_up=PasskeyApprovalStepUp(
            database, clock=clock, timeout_seconds=timeout
        ),
    )


def install_auth(
    app: FastAPI, auth: AuthServices, *, settings: Settings, database: Database
) -> None:
    """Attach the services and make the session cookie the way to be authenticated.

    Replaces the anonymous-only provider of ``install_authz`` with the session
    provider, and gives the authorizer the directory it re-reads an Agent's
    delegating user from.
    """
    app.state.auth = auth
    install_authz(
        app,
        settings=settings,
        database=database,
        principal_provider=auth.provider,
        principal_directory=auth.directory,
        audit_sink=auth.audit_sink,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
