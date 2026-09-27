"""HTTP fixtures for the Passkey routes: the software authenticator over the wire."""

from .auth_http_support import HttpTestCase, cookie_of, error_code
from .passkey_pg_support import ADMIN_PASSWORD, OWNER_PASSWORD, PASSKEY_SETTINGS, device
from .passkey_support import SoftwareAuthenticator

API = "/api/v1/auth"
PASSKEYS = f"{API}/passkeys"

__all__ = [
    "ADMIN_PASSWORD",
    "API",
    "OWNER_PASSWORD",
    "PASSKEYS",
    "PasskeyHttpTestCase",
    "cookie_of",
    "error_code",
]


class PasskeyHttpTestCase(HttpTestCase):
    """The application with Passkeys configured; helpers for the ceremonies."""

    settings_overrides = PASSKEY_SETTINGS

    def owner(self, name: str = "boss"):
        return self.make_user(name, role="owner", password=OWNER_PASSWORD)

    def admin(self, name: str = "admin-one"):
        return self.make_user(name, role="admin", password=ADMIN_PASSWORD)

    def gate_of(self, token: str) -> str:
        return self.call("GET", f"{API}/session", token=token).json()["auth"][
            "passkey"
        ]["gate"]

    # -- ceremonies over HTTP ---------------------------------------------------------

    def register_begin(self, token: str, **options):
        return self.call("POST", f"{PASSKEYS}/enroll/begin", token=token, **options)

    def register(
        self,
        token: str,
        authenticator: SoftwareAuthenticator | None = None,
        *,
        name: str | None = None,
        **create,
    ):
        """begin + the authenticator's answer + finish. Returns (response, token).

        ``token`` after the call is the response's new cookie if it set one, else
        the old one (registering an already open session does not rotate it).
        """
        authenticator = authenticator or device()
        begun = self.register_begin(token)
        self.assertEqual(begun.status_code, 200, begun.text)
        answer = authenticator.create(begun.json()["options"], **create)
        body = {"credential": answer}
        if name is not None:
            body["name"] = name
        response = self.call(
            "POST", f"{PASSKEYS}/enroll/finish", token=token, json=body
        )
        return response, self.token_after(response, token)

    def authenticate(self, token: str, authenticator: SoftwareAuthenticator, **get):
        begun = self.call("POST", f"{PASSKEYS}/authenticate/begin", token=token)
        self.assertEqual(begun.status_code, 200, begun.text)
        answer = authenticator.get(begun.json()["options"], **get)
        response = self.call(
            "POST",
            f"{PASSKEYS}/authenticate/finish",
            token=token,
            json={"credential": answer},
        )
        return response, self.token_after(response, token)

    def token_after(self, response, old: str) -> str:
        jar = cookie_of(response)
        from paw_backend.auth.limits import SESSION_COOKIE_NAME

        if SESSION_COOKIE_NAME in jar and jar[SESSION_COOKIE_NAME].value:
            return jar[SESSION_COOKIE_NAME].value
        return old

    def enrolled(
        self, name: str = "boss", password: str = OWNER_PASSWORD, role="owner"
    ):
        """A user, signed in, with one Passkey registered: (token, authenticator)."""
        self.make_user(name, role=role, password=password)
        token = self.login_token(name, password)
        authenticator = device()
        response, token = self.register(token, authenticator)
        self.assertEqual(response.status_code, 200, response.text)
        return token, authenticator

    def stepped_up(
        self, name: str = "boss", password: str = OWNER_PASSWORD, role="owner"
    ):
        token, authenticator = self.enrolled(name, password, role)
        response, token = self.authenticate(token, authenticator)
        self.assertEqual(response.status_code, 200, response.text)
        return token, authenticator
