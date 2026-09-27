"""What a browser may send: the strict shape of a WebAuthn answer (no database)."""

import copy
import os
import unittest

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.passkeys import ceremony, types
from paw_backend.auth.passkeys.config import PasskeyConfig

from .passkey_pg_support import ORIGIN, RP_ID, device
from .passkey_support import b64url

CONFIG = PasskeyConfig(RP_ID, "PAW", (ORIGIN,), 300)


def registration_answer() -> dict:
    challenge = os.urandom(32)
    options = ceremony.registration_options(
        CONFIG,
        user_id=__import__("uuid").uuid4(),
        login_name="alice",
        challenge=challenge,
        exclude=[],
    )
    return device().create(options)


def assertion_answer() -> dict:
    import uuid

    authenticator = device()
    user = uuid.uuid4()
    options = ceremony.registration_options(
        CONFIG, user_id=user, login_name="alice", challenge=os.urandom(32), exclude=[]
    )
    authenticator.create(options)
    request = ceremony.authentication_options(
        CONFIG,
        challenge=os.urandom(32),
        allow=[authenticator.credentials[0].credential_id],
    )
    return authenticator.get(request)


class Base64UrlTest(unittest.TestCase):
    def decode(self, value, low=1, high=8):
        return types.decode_base64url("x", value, min_bytes=low, max_bytes=high)

    def test_a_canonical_value_round_trips(self):
        for raw in (b"a", b"ab", b"abc", b"\x00\xff\xfe", os.urandom(8)):
            with self.subTest(raw=raw):
                self.assertEqual(self.decode(b64url(raw)), raw)

    def test_everything_else_is_refused_with_a_typed_error(self):
        cases = {
            "padding": "YQ==",
            "a plus": "+w",
            "a slash": "/w",
            "a space": "YQ ",
            "a newline": "YQ\n",
            "the empty string": "",
            "one character": "Y",
            "trailing bits set": "YR",  # decodes as b"a" but is not canonical
            "unicode": "Yé",
            "a number": 5,
            "bytes": b"YQ",
            "none": None,
            "a list": ["YQ"],
            "too long": b64url(b"x" * 9),
        }
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(InvalidAuthInputError) as caught:
                    self.decode(value)
                self.assertEqual(caught.exception.field, "x")

    def test_the_size_is_bounded_before_decoding(self):
        # 8 bytes is 11 characters; twelve is over the bound whatever they decode to.
        with self.assertRaises(InvalidAuthInputError):
            self.decode("A" * 12)
        self.assertEqual(self.decode(b64url(b"x" * 8)), b"x" * 8)
        with self.assertRaises(InvalidAuthInputError):
            self.decode(b64url(b"x"), low=2)


class RegistrationParseTest(unittest.TestCase):
    def test_a_real_answer_is_parsed_and_rebuilt_from_checked_values(self):
        answer = registration_answer()
        checked = types.parse_registration_credential(answer)
        rebuilt = checked.as_json()
        self.assertEqual(
            set(rebuilt), {"id", "rawId", "type", "response"}
        )  # nothing else is passed on
        self.assertEqual(
            set(rebuilt["response"]), {"clientDataJSON", "attestationObject"}
        )
        self.assertEqual(rebuilt["id"], answer["id"])
        self.assertEqual(
            rebuilt["response"]["attestationObject"],
            answer["response"]["attestationObject"],
        )

    def test_the_repr_hides_the_payloads(self):
        answer = registration_answer()
        text = repr(types.parse_registration_credential(answer))
        self.assertNotIn(answer["response"]["attestationObject"][:20], text)
        self.assertNotIn(answer["response"]["clientDataJSON"][:20], text)

    def test_hostile_shapes(self):
        answer = registration_answer()

        def with_(**changes):
            value = copy.deepcopy(answer)
            for path, new in changes.items():
                target = value
                *parents, last = path.split(".")
                for parent in parents:
                    target = target[parent]
                if new is KeyError:
                    del target[last]
                else:
                    target[last] = new
            return value

        cases = {
            "not a dict": [answer],
            "a dict subclass": type("D", (dict,), {})(answer),
            "no type": with_(type=KeyError),
            "another type": with_(type="password"),
            "no id": with_(id=KeyError),
            "no raw id": with_(rawId=KeyError),
            "id differs from raw id": with_(id=b64url(b"\x01" * 32)),
            "a raw id too short": with_(
                rawId=b64url(b"\x01" * 15), id=b64url(b"\x01" * 15)
            ),
            "a raw id too long": with_(
                rawId=b64url(b"\x01" * 1024), id=b64url(b"\x01" * 1024)
            ),
            "no response": with_(response=KeyError),
            "a response that is a list": with_(response=[]),
            "no client data": with_(**{"response.clientDataJSON": KeyError}),
            "client data that is a number": with_(**{"response.clientDataJSON": 1}),
            "client data too big": with_(
                **{"response.clientDataJSON": b64url(b"x" * 2049)}
            ),
            "an empty attestation object": with_(**{"response.attestationObject": ""}),
            "an attestation object too big": with_(
                **{"response.attestationObject": b64url(b"x" * 4097)}
            ),
            "extensions as a list": with_(clientExtensionResults=[]),
            "too many extensions": with_(
                clientExtensionResults={str(i): 0 for i in range(17)}
            ),
        }
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(InvalidAuthInputError) as caught:
                    types.parse_registration_credential(value)
                self.assertEqual(caught.exception.field, "credential")
                self.assertNotIn("password", str(caught.exception))

    def test_the_limits_are_accepted_exactly(self):
        answer = registration_answer()
        answer["response"]["clientDataJSON"] = b64url(b"{" + b"x" * 2046 + b"}")
        types.parse_registration_credential(answer)
        answer["response"]["clientDataJSON"] = b64url(b"{" + b"x" * 2047 + b"}")
        with self.assertRaises(InvalidAuthInputError):
            types.parse_registration_credential(answer)
        answer = registration_answer()
        answer["clientExtensionResults"] = {str(i): 0 for i in range(16)}
        types.parse_registration_credential(answer)


class AssertionParseTest(unittest.TestCase):
    def test_a_real_answer_is_parsed_and_rebuilt(self):
        answer = assertion_answer()
        checked = types.parse_assertion_credential(answer)
        rebuilt = checked.as_json()
        self.assertEqual(
            rebuilt["response"],
            {
                "clientDataJSON": answer["response"]["clientDataJSON"],
                "authenticatorData": answer["response"]["authenticatorData"],
                "signature": answer["response"]["signature"],
                "userHandle": answer["response"]["userHandle"],
            },
        )
        self.assertEqual(
            checked.user_handle,
            types.decode_base64url(
                "x", answer["response"]["userHandle"], min_bytes=1, max_bytes=64
            ),
        )

    def test_the_user_handle_is_optional_and_bounded(self):
        answer = assertion_answer()
        for absent in (None, ""):
            with self.subTest(handle=repr(absent)):
                answer["response"]["userHandle"] = absent
                self.assertIsNone(types.parse_assertion_credential(answer).user_handle)
        del answer["response"]["userHandle"]
        self.assertIsNone(types.parse_assertion_credential(answer).user_handle)
        for bad in (b64url(b"x" * 65), 5, ["a"], "YR", "+"):
            with self.subTest(handle=repr(bad)[:20]):
                answer["response"]["userHandle"] = bad
                with self.assertRaises(InvalidAuthInputError):
                    types.parse_assertion_credential(answer)
        answer["response"]["userHandle"] = b64url(b"x" * 64)
        self.assertEqual(
            types.parse_assertion_credential(answer).user_handle, b"x" * 64
        )

    def test_hostile_shapes(self):
        answer = assertion_answer()
        for path, new in (
            ("response.signature", "YQ"),  # under the minimum
            ("response.signature", b64url(b"x" * 1025)),
            ("response.authenticatorData", b64url(b"x" * 36)),
            ("response.authenticatorData", b64url(b"x" * 1025)),
            ("response.authenticatorData", None),
            ("response.clientDataJSON", ""),
            ("rawId", 5),
        ):
            with self.subTest(path=path, value=repr(new)[:20]):
                value = copy.deepcopy(answer)
                target = value
                *parents, last = path.split(".")
                for parent in parents:
                    target = target[parent]
                target[last] = new
                with self.assertRaises(InvalidAuthInputError):
                    types.parse_assertion_credential(value)

    def test_the_repr_hides_the_signature(self):
        answer = assertion_answer()
        text = repr(types.parse_assertion_credential(answer))
        for key in ("signature", "authenticatorData", "clientDataJSON"):
            self.assertNotIn(answer["response"][key][:16], text)
