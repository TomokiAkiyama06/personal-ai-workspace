"""Fixtures for the Passkey tests that need PostgreSQL (services and HTTP)."""

import uuid
from dataclasses import dataclass

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys.types import parse_assertion_credential
from paw_backend.auth.service import LoginResult
from paw_backend.auth.sessions import AuthenticatedSession
from paw_backend.auth.state import StepUpEvidence

from .auth_support import PASSWORD, PostgresAuthTestCase
from .passkey_support import SoftwareAuthenticator

RP_ID = "paw.example.test"
ORIGIN = "https://paw.example.test"
PASSKEY_SETTINGS = {"passkey_rp_id": RP_ID, "passkey_origins": [ORIGIN]}
SOURCE = "203.0.113.7"
OWNER_PASSWORD = "owner passphrase"
ADMIN_PASSWORD = "admin passphrase"


def context(source: str = SOURCE) -> RequestContext:
    return RequestContext(uuid.uuid4(), tokens.source_bucket(source))


def device(**options) -> SoftwareAuthenticator:
    return SoftwareAuthenticator(origin=ORIGIN, rp_id=RP_ID, **options)


@dataclass
class Registered:
    """The outcome of a registration ceremony driven through the service."""

    result: object
    # The session afterwards (its id rotates when registering opens the gate).
    auth: AuthenticatedSession
    login: LoginResult | None

    @property
    def passkey_id(self) -> uuid.UUID:
        return self.result.passkey.id


class PasskeyTestCase(PostgresAuthTestCase):
    """A migrated database with Passkeys configured, and helpers for the ceremonies."""

    settings_overrides = PASSKEY_SETTINGS

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.passkeys = self.services.passkeys
        self.registry = self.services.passkeys._registry

    # -- people ---------------------------------------------------------------------

    async def make_owner(self, name: str = "boss"):
        return await self.make_user(name, role="owner", password=OWNER_PASSWORD)

    async def make_admin(self, name: str = "admin-one"):
        return await self.make_user(name, role="admin", password=ADMIN_PASSWORD)

    async def sign_in(
        self, user, *, password: str | None = None, source: str = SOURCE, **options
    ) -> AuthenticatedSession:
        """A password sign-in; the session is restricted if the policy says so."""
        result = await self.auth.login(
            user.login_name, password or user.password, context(source), **options
        )
        return result.session

    # -- ceremonies -----------------------------------------------------------------

    async def register(
        self,
        auth: AuthenticatedSession,
        authenticator: SoftwareAuthenticator | None = None,
        *,
        name: str | None = None,
        ctx: RequestContext | None = None,
        **create,
    ) -> Registered:
        """begin, the authenticator's answer, finish. ``create`` doctors the answer."""
        authenticator = authenticator or device()
        ctx = ctx or context()
        options = await self.passkeys.register_begin(auth, ctx)
        answer = authenticator.create(options, **create)
        result = await self.passkeys.register_finish(auth, answer, name, ctx)
        return Registered(
            result, result.login.session if result.login else auth, result.login
        )

    async def authenticate(
        self,
        auth: AuthenticatedSession,
        authenticator: SoftwareAuthenticator,
        *,
        ctx: RequestContext | None = None,
        **get,
    ) -> LoginResult:
        """begin, the authenticator's answer, ``step_up``. Returns the new session."""
        ctx = ctx or context()
        options = await self.passkeys.authenticate_begin(auth, ctx)
        answer = authenticator.get(options, **get)
        return await self.step_up_with(auth, answer, ctx)

    async def step_up_with(
        self,
        auth: AuthenticatedSession,
        answer: dict,
        ctx: RequestContext | None = None,
    ) -> LoginResult:
        evidence = StepUpEvidence(
            AuthMethod.PASSKEY, assertion=parse_assertion_credential(answer)
        )
        return await self.auth.step_up(auth, evidence, ctx or context())

    async def enrolled_session(self, user, password: str | None = None, **options):
        """Sign in and register a first Passkey: (open session, authenticator)."""
        auth = await self.sign_in(user, password=password, **options)
        authenticator = device()
        registered = await self.register(auth, authenticator)
        return registered.auth, authenticator

    async def fully_stepped_up(self, user, password: str | None = None):
        """An open session with a fresh Passkey step-up, and its authenticator."""
        auth, authenticator = await self.enrolled_session(user, password)
        stepped = await self.authenticate(auth, authenticator)
        return stepped.session, authenticator

    # -- reading the state ------------------------------------------------------------

    async def passkey_rows(self, user_id: uuid.UUID | None = None) -> list:
        sql = "SELECT * FROM user_passkeys"
        params = {}
        if user_id is not None:
            sql += " WHERE user_id = :u"
            params["u"] = user_id
        return await self.query(sql + " ORDER BY created_at, id", **params)

    async def session_row(self, session_id: uuid.UUID):
        rows = await self.query(
            "SELECT * FROM auth_sessions WHERE id = :i", i=session_id
        )
        return rows[0]

    async def everything_stored(self) -> str:
        parts = [await super().everything_stored()]
        for table in ("user_passkeys", "passkey_challenges"):
            parts += [
                str(row[0])
                for row in await self.query(f"SELECT t::text FROM {table} t")
            ]
        return "\n".join(parts)


__all__ = [
    "ADMIN_PASSWORD",
    "ORIGIN",
    "OWNER_PASSWORD",
    "PASSWORD",
    "PASSKEY_SETTINGS",
    "RP_ID",
    "PasskeyTestCase",
    "Registered",
    "context",
    "device",
]
