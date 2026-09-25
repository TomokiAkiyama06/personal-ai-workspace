"""The WebAuthn ceremonies: options and verification (no database).

Every payload is made by ``passkey_support.SoftwareAuthenticator``, which follows
the W3C specification and shares no code with the library that verifies it.
"""

import os
import unittest
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec

from paw_backend.auth.passkeys import ceremony
from paw_backend.auth.passkeys.config import PasskeyConfig
from paw_backend.auth.passkeys.types import (
    parse_assertion_credential,
    parse_registration_credential,
)

from .identity_support import LogCapture
from .passkey_pg_support import ORIGIN, RP_ID, device
from .passkey_support import (
    AT,
    BE,
    BS,
    ED,
    EDDSA,
    ES256,
    RS256,
    UP,
    UV,
    b64url,
    cbor,
)

CONFIG = PasskeyConfig(RP_ID, "PAW", (ORIGIN, "https://app.paw.example.test"), 300)
USER = uuid.uuid4()


def begin(config=CONFIG, exclude=()):
    challenge = os.urandom(32)
    options = ceremony.registration_options(
        config, user_id=USER, login_name="alice", challenge=challenge, exclude=exclude
    )
    return challenge, options


def registered(authenticator=None, **create):
    authenticator = authenticator or device()
    challenge, options = begin()
    answer = authenticator.create(options, **create)
    verified = ceremony.verify_registration(
        CONFIG, parse_registration_credential(answer), challenge
    )
    return authenticator, verified


def request(verified, config=CONFIG):
    challenge = os.urandom(32)
    options = ceremony.authentication_options(
        config, challenge=challenge, allow=[verified.credential_id]
    )
    return challenge, options


class OptionsTest(unittest.TestCase):
    def test_registration_options(self):
        exclude = [b"\x01" * 16, b"\x02" * 32]
        challenge, options = begin(exclude=exclude)
        self.assertEqual(options["challenge"], b64url(challenge))
        self.assertEqual(options["rp"], {"name": "PAW", "id": RP_ID})
        self.assertEqual(options["user"]["id"], b64url(USER.bytes))
        self.assertEqual(options["attestation"], "none")
        self.assertEqual(
            options["authenticatorSelection"]["userVerification"], "required"
        )
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "preferred")
        self.assertEqual(
            [p["alg"] for p in options["pubKeyCredParams"]], [EDDSA, ES256, RS256]
        )
        self.assertEqual(
            [c["id"] for c in options["excludeCredentials"]],
            [b64url(x) for x in exclude],
        )
        self.assertEqual(options["timeout"], 300_000)

    def test_authentication_options(self):
        challenge = os.urandom(32)
        ids = [b"\x03" * 16]
        options = ceremony.authentication_options(
            CONFIG, challenge=challenge, allow=ids
        )
        self.assertEqual(
            options,
            {
                "challenge": b64url(challenge),
                "timeout": 300_000,
                "rpId": RP_ID,
                "allowCredentials": [{"id": b64url(ids[0]), "type": "public-key"}],
                "userVerification": "required",
            },
        )


class RegistrationTest(unittest.TestCase):
    def test_every_algorithm_verifies_and_reports_the_credential(self):
        for alg in (ES256, EDDSA, RS256):
            with self.subTest(alg=alg):
                authenticator, verified = registered(alg=alg)
                stored = authenticator.credentials[0]
                self.assertEqual(verified.credential_id, stored.credential_id)
                self.assertEqual(verified.sign_count, 0)
                self.assertIsNone(verified.aaguid)
                self.assertEqual(
                    (verified.backup_eligible, verified.backed_up), (False, False)
                )

    def test_backup_flags_and_the_model_are_reported(self):
        model = uuid.uuid4()
        _, verified = registered(
            device(backup_eligible=True, backed_up=True), aaguid=model.bytes
        )
        self.assertEqual(
            (verified.backup_eligible, verified.backed_up, verified.aaguid),
            (True, True, model),
        )
        _, verified = registered(device(backup_eligible=True, backed_up=False))
        self.assertEqual((verified.backup_eligible, verified.backed_up), (True, False))

    def test_a_backed_up_credential_that_is_not_eligible_is_refused(self):
        with self.assertRaises(ceremony.CeremonyRejected):
            registered(flags=UP | UV | AT | BS)

    def test_a_second_origin_of_the_configuration_is_accepted(self):
        registered(origin="https://app.paw.example.test")

    def test_the_initial_counter_is_kept_and_bounded(self):
        _, verified = registered(initial_count=41)
        self.assertEqual(verified.sign_count, 41)

    def hostile(self, **create):
        challenge, options = begin()
        answer = device().create(options, **create)
        with self.assertRaises(ceremony.CeremonyRejected):
            ceremony.verify_registration(
                CONFIG, parse_registration_credential(answer), challenge
            )

    def test_hostile_registrations(self):
        cases = {
            "another origin": {"origin": "https://evil.example"},
            "an origin of the RP ID's parent": {"origin": "https://test"},
            "http": {"origin": "http://paw.example.test"},
            "another port": {"origin": ORIGIN + ":444"},
            "another rp id hash": {"rp_id": "evil.example"},
            "no user verification": {"flags": UP | AT},
            "no user presence": {"flags": UV | AT},
            "no credential data": {"flags": UP | UV},
            "extension flag without extensions": {"flags": UP | UV | AT | ED},
            "an assertion type": {"client_data_type": "webauthn.get"},
            "a cross-origin frame": {"client_data_extra": {"crossOrigin": True}},
            "a top origin": {"client_data_extra": {"topOrigin": ORIGIN}},
            "a packed statement": {
                "fmt": "packed",
                "att_stmt": {"alg": -7, "sig": b"x" * 8},
            },
            "a none statement with content": {"att_stmt": {"x": 1}},
            "a made-up format": {"fmt": "made-up"},
            "a tpm statement": {"fmt": "tpm", "att_stmt": {"ver": "2.0"}},
        }
        for label, create in cases.items():
            with self.subTest(label):
                self.hostile(**create)

    def test_a_valid_self_attestation_is_refused_though_the_library_accepts_it(self):
        """A real authenticator may answer "packed" although "none" was asked for."""
        from webauthn import verify_registration_response

        challenge, options = begin()
        answer = device().create(options, self_attest=True)
        # The library alone verifies it (so the refusal below is OUR policy) ...
        verified = verify_registration_response(
            credential=answer,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=[ORIGIN],
            require_user_verification=True,
        )
        self.assertEqual(verified.fmt, "packed")
        # ... and the server does not take it.
        with self.assertRaises(ceremony.CeremonyRejected):
            ceremony.verify_registration(
                CONFIG, parse_registration_credential(answer), challenge
            )

    def test_the_wrong_challenge(self):
        challenge, options = begin()
        answer = device().create(options)
        for wrong in (os.urandom(32), challenge[:-1], challenge + b"\x00", b""):
            with self.subTest(length=len(wrong)):
                with self.assertRaises(ceremony.CeremonyRejected):
                    ceremony.verify_registration(
                        CONFIG, parse_registration_credential(answer), wrong
                    )

    def test_an_attestation_object_with_more_than_the_three_members(self):
        challenge, options = begin()
        answer = device().create(options)
        from .passkey_support import unb64url

        raw = unb64url(answer["response"]["attestationObject"])
        # Rebuild it with a fourth member (canonical CBOR of a 4-entry map).
        auth_data = raw[raw.index(b"authData") + len(b"authData") + 2 :]
        crafted = cbor(
            {"fmt": "none", "attStmt": {}, "authData": auth_data, "extra": 1}
        )
        answer["response"]["attestationObject"] = b64url(crafted)
        with self.assertRaises(ceremony.CeremonyRejected):
            ceremony.verify_registration(
                CONFIG, parse_registration_credential(answer), challenge
            )

    def test_an_algorithm_the_server_did_not_offer_is_refused(self):
        challenge, options = begin()
        key = ec.generate_private_key(ec.SECP384R1())
        numbers = key.public_key().public_numbers()
        cose = {
            1: 2,
            3: -35,
            -1: 2,
            -2: numbers.x.to_bytes(48, "big"),
            -3: numbers.y.to_bytes(48, "big"),
        }
        with patch("tests.passkey_support.new_key", return_value=(key, cose)):
            answer = device().create(options)
        with self.assertRaises(ceremony.CeremonyRejected):
            ceremony.verify_registration(
                CONFIG, parse_registration_credential(answer), challenge
            )

    def test_garbage_is_a_rejection_never_another_exception(self):
        challenge, options = begin()
        answer = device().create(options)
        for junk in (
            b"\xff" * 40,
            b"\xa0\x00",
            b"\x00\x00",
            cbor([1, 2]),
            cbor("text"),
            cbor({"fmt": 1}),
        ):
            with self.subTest(junk=junk[:6]):
                doctored = {
                    **answer,
                    "response": {
                        **answer["response"],
                        "attestationObject": b64url(junk),
                    },
                }
                with self.assertRaises(ceremony.CeremonyRejected):
                    ceremony.verify_registration(
                        CONFIG, parse_registration_credential(doctored), challenge
                    )
        for junk in (b"not json", b"[]", b"12", b'"x"', b"{ "):
            with self.subTest(client_data=junk):
                doctored = {
                    **answer,
                    "response": {**answer["response"], "clientDataJSON": b64url(junk)},
                }
                with self.assertRaises(ceremony.CeremonyRejected):
                    ceremony.verify_registration(
                        CONFIG, parse_registration_credential(doctored), challenge
                    )

    def test_a_rejection_says_nothing_and_logs_nothing_of_the_answer(self):
        challenge, options = begin()
        answer = device().create(options, origin="https://evil.example")
        with LogCapture() as logs:
            with self.assertRaises(ceremony.CeremonyRejected) as caught:
                ceremony.verify_registration(
                    CONFIG, parse_registration_credential(answer), challenge
                )
        self.assertEqual(str(caught.exception), "")
        self.assertNotIn("evil.example", logs.text)
        self.assertNotIn(b64url(challenge), logs.text)
        self.assertNotIn(answer["id"], logs.text)

    def test_the_arguments_are_typed(self):
        challenge, options = begin()
        with self.assertRaises(TypeError):
            ceremony.verify_registration(CONFIG, options, challenge)


class AssertionTest(unittest.TestCase):
    def setUp(self):
        self.authenticator, self.verified = registered()
        self.key = self.verified.public_key

    def verify(self, answer, challenge, **kwargs):
        return ceremony.verify_assertion(
            CONFIG,
            parse_assertion_credential(answer),
            challenge,
            public_key=kwargs.pop("public_key", self.key),
            user_id=kwargs.pop("user_id", USER),
        )

    def test_every_algorithm_verifies_an_assertion(self):
        for alg in (ES256, EDDSA, RS256):
            with self.subTest(alg=alg):
                authenticator, verified = registered(alg=alg)
                challenge, options = request(verified)
                answer = authenticator.get(options)
                result = self.verify(answer, challenge, public_key=verified.public_key)
                self.assertEqual(result.sign_count, 1)

    def test_the_counter_and_backup_state_are_reported_not_judged(self):
        challenge, options = request(self.verified)
        # A counter that goes DOWN is still reported (the database judges it).
        answer = self.authenticator.get(options, count=0)
        self.assertEqual(self.verify(answer, challenge).sign_count, 0)
        challenge, options = request(self.verified)
        answer = self.authenticator.get(options, count=4_294_967_295)
        self.assertEqual(self.verify(answer, challenge).sign_count, 4_294_967_295)
        flags = UP | UV | BE | BS
        challenge, options = request(self.verified)
        answer = self.authenticator.get(options, flags=flags, count=3)
        result = self.verify(answer, challenge)
        self.assertEqual((result.backup_eligible, result.backed_up), (True, True))

    def test_a_user_handle_must_be_the_users(self):
        challenge, options = request(self.verified)
        good = self.authenticator.get(options)
        self.verify(good, challenge)
        for handle in (None, USER.bytes):  # absent or right: accepted
            challenge, options = request(self.verified)
            self.verify(self.authenticator.get(options, user_handle=handle), challenge)
        for handle in (uuid.uuid4().bytes, b"x", USER.bytes + b"\x00"):
            with self.subTest(handle=handle[:4]):
                challenge, options = request(self.verified)
                answer = self.authenticator.get(options, user_handle=handle)
                with self.assertRaises(ceremony.CeremonyRejected):
                    self.verify(answer, challenge)

    def test_hostile_assertions(self):
        cases = {
            "another origin": {"origin": "https://evil.example"},
            "http": {"origin": "http://paw.example.test"},
            "another rp id hash": {"rp_id": "evil.example"},
            "no user verification": {"flags": UP},
            "no user presence": {"flags": UV},
            "a registration type": {"client_data_type": "webauthn.create"},
            "a cross-origin frame": {"client_data_extra": {"crossOrigin": True}},
            "a top origin": {"client_data_extra": {"topOrigin": ORIGIN}},
            "another challenge": {"challenge": b"\x05" * 32},
        }
        for label, get in cases.items():
            with self.subTest(label):
                challenge, options = request(self.verified)
                answer = self.authenticator.get(options, **get)
                with self.assertRaises(ceremony.CeremonyRejected):
                    self.verify(answer, challenge)

    def test_a_signature_of_another_key_or_over_other_data_is_refused(self):
        challenge, options = request(self.verified)
        answer = self.authenticator.get(
            options, sign_with=ec.generate_private_key(ec.SECP256R1())
        )
        with self.assertRaises(ceremony.CeremonyRejected):
            self.verify(answer, challenge)
        # The right signature checked against another credential's key.
        _, other = registered()
        challenge, options = request(self.verified)
        answer = self.authenticator.get(options)
        with self.assertRaises(ceremony.CeremonyRejected):
            self.verify(answer, challenge, public_key=other.public_key)

    def test_a_flipped_bit_anywhere_in_the_signed_data_is_refused(self):
        from .passkey_support import unb64url

        challenge, options = request(self.verified)
        answer = self.authenticator.get(options)
        for part in ("authenticatorData", "clientDataJSON", "signature"):
            for index in (0, -1):
                with self.subTest(part=part, index=index):
                    raw = bytearray(unb64url(answer["response"][part]))
                    raw[index] ^= 0x01
                    doctored = {
                        **answer,
                        "response": {**answer["response"], part: b64url(bytes(raw))},
                    }
                    with self.assertRaises(ceremony.CeremonyRejected):
                        self.verify(doctored, challenge)

    def test_a_rejection_says_nothing_and_logs_nothing_of_the_answer(self):
        challenge, options = request(self.verified)
        answer = self.authenticator.get(options, origin="https://evil.example")
        with LogCapture() as logs:
            with self.assertRaises(ceremony.CeremonyRejected) as caught:
                self.verify(answer, challenge)
        self.assertEqual(str(caught.exception), "")
        for secret in (
            "evil.example",
            b64url(challenge),
            answer["response"]["signature"],
        ):
            self.assertNotIn(secret, logs.text)

    def test_the_arguments_are_typed(self):
        challenge, options = request(self.verified)
        with self.assertRaises(TypeError):
            ceremony.verify_assertion(
                CONFIG,
                self.authenticator.get(options),
                challenge,
                public_key=self.key,
                user_id=USER,
            )
