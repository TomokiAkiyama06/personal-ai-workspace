"""The opaque keyset cursor of the administrator's project list (Issue #84).

Pure functions, no database. The cursor is text a caller hands back, so it is
tested as hostile input: every shape that is not exactly what ``encode_cursor``
writes for the same filter is refused with the typed error, a closed problem and
no echo of the value, and everything that is accepted round-trips.
"""

import base64
import random
import unittest
import uuid
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.projects import InputProblem, InvalidProjectInputError, ProjectStatus
from paw_backend.projects.cursor import (
    MAX_MICROS,
    MIN_MICROS,
    Keyset,
    decode_cursor,
    encode_cursor,
    filter_token,
)
from paw_backend.projects.limits import MAX_CURSOR_CHARS

from .projects_support import T0

S = ProjectStatus
FILTERS = (None, S.ACTIVE, S.ARCHIVED, S.PENDING_DELETION)
ID = uuid.UUID("0190b6f0-1234-7abc-8def-0123456789ab")
AT = datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=UTC)
# The exact text of the two known cursors below (the format is version 1).
KNOWN_ALL = (
    "MS5hbGwuMTc5MDI1MTIwMDEyMzQ1Ni4wMTkwYjZmMC0xMjM0LTdhYmMtOGRlZi0wMTIzNDU2Nzg5YWI"
)
KNOWN_PENDING = (
    "MS5wZW5kaW5nX2RlbGV0aW9uLjE3OTAyNTEyMDAxMjM0NTYuMDE5MGI2ZjAtMTIzNC03YWJjLThkZWYt"
    "MDEyMzQ1Njc4OWFi"
)


def b64(text: str) -> str:
    """The unpadded URL-safe base64 of ``text`` (how a cursor is spelled)."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def token(
    filter_: str = "all",
    micros: object = 1790251200123456,
    ident: object = ID,
    version: str = "1",
) -> str:
    return f"{version}.{filter_}.{micros}.{ident}"


class CursorCodecTest(unittest.TestCase):
    def test_the_format_is_pinned_by_known_values(self):
        self.assertEqual(encode_cursor(None, AT, ID), KNOWN_ALL)
        self.assertEqual(encode_cursor(S.PENDING_DELETION, AT, ID), KNOWN_PENDING)
        self.assertEqual(decode_cursor(KNOWN_ALL, None), Keyset(AT, ID))
        self.assertEqual(
            decode_cursor(KNOWN_PENDING, S.PENDING_DELETION), Keyset(AT, ID)
        )
        self.assertEqual(
            base64.urlsafe_b64decode(KNOWN_ALL + "=").decode(),
            "1.all.1790251200123456.0190b6f0-1234-7abc-8def-0123456789ab",
        )

    def test_the_filter_tokens_are_the_closed_vocabulary(self):
        self.assertEqual(
            [filter_token(status) for status in FILTERS],
            ["all", "active", "archived", "pending_deletion"],
        )

    def test_every_position_round_trips_for_every_filter(self):
        instants = (
            datetime(1970, 1, 1, tzinfo=UTC),
            datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            T0,
            AT,
            AT.replace(microsecond=1),
            datetime.min.replace(tzinfo=UTC),
            datetime.max.replace(tzinfo=UTC),
        )
        ids = (ID, uuid.UUID(int=0), uuid.UUID(int=(1 << 128) - 1), uuid.uuid4())
        for status in FILTERS:
            for instant in instants:
                for ident in ids:
                    with self.subTest(status=status, instant=instant, ident=ident):
                        cursor = encode_cursor(status, instant, ident)
                        self.assertLessEqual(len(cursor), MAX_CURSOR_CHARS)
                        self.assertEqual(
                            decode_cursor(cursor, status), Keyset(instant, ident)
                        )

    def test_an_instant_in_another_time_zone_is_the_same_position(self):
        tokyo = timezone(timedelta(hours=9))
        local = AT.astimezone(tokyo)
        self.assertEqual(encode_cursor(None, local, ID), KNOWN_ALL)
        decoded = decode_cursor(KNOWN_ALL, None)
        self.assertEqual(decoded.created_at, local)
        self.assertEqual(decoded.created_at.utcoffset(), timedelta(0))

    def test_the_longest_valid_cursor_is_exactly_the_limit(self):
        worst = uuid.UUID(int=(1 << 128) - 1)
        lengths = {
            len(encode_cursor(S.PENDING_DELETION, instant, worst))
            for instant in (
                datetime.max.replace(tzinfo=UTC),
                datetime.min.replace(tzinfo=UTC),
            )
        }
        self.assertEqual(lengths, {MAX_CURSOR_CHARS})
        for status in FILTERS:
            self.assertLessEqual(
                len(encode_cursor(status, datetime.max.replace(tzinfo=UTC), worst)),
                MAX_CURSOR_CHARS,
            )

    def test_the_representable_instants_are_the_datetime_range(self):
        self.assertEqual(
            decode_cursor(b64(token(micros=MIN_MICROS)), None).created_at,
            datetime.min.replace(tzinfo=UTC),
        )
        self.assertEqual(
            decode_cursor(b64(token(micros=MAX_MICROS)), None).created_at,
            datetime.max.replace(tzinfo=UTC),
        )


class HostileCursorTest(unittest.TestCase):
    def assertRefused(self, value, problem, status=None):
        with self.assertRaises(InvalidProjectInputError) as raised:
            decode_cursor(value, status)
        self.assertEqual(raised.exception.field, "cursor")
        self.assertIs(raised.exception.problem, problem)
        if isinstance(value, str) and len(value) > 3:
            # The value is never echoed.
            self.assertNotIn(value, str(raised.exception))
            self.assertNotIn(value, repr(raised.exception.args))

    def test_only_an_exact_str_is_a_cursor(self):
        class Sub(str):
            pass

        for bad in (
            None,
            b"",
            KNOWN_ALL.encode(),
            bytearray(KNOWN_ALL.encode()),
            0,
            1,
            True,
            1.5,
            [KNOWN_ALL],
            {"cursor": KNOWN_ALL},
            ID,
            object(),
            Sub(KNOWN_ALL),
        ):
            with self.subTest(bad=repr(bad)):
                self.assertRefused(bad, InputProblem.NOT_A_STRING)

    def test_text_longer_than_any_cursor_is_refused_before_it_is_looked_at(self):
        for bad in (
            KNOWN_ALL + "A" * (MAX_CURSOR_CHARS - len(KNOWN_ALL) + 1),
            "A" * (MAX_CURSOR_CHARS + 1),
            "A" * 100_000,
            "\x00" * 10_000,
        ):
            with self.subTest(length=len(bad)):
                self.assertRefused(bad, InputProblem.TOO_LONG)
        # The limit itself is not "too long": it fails on its content instead.
        self.assertRefused("A" * MAX_CURSOR_CHARS, InputProblem.INVALID_CURSOR)

    def test_text_that_is_not_url_safe_base64_is_refused(self):
        for name, bad in {
            "empty": "",
            "space": " ",
            "newline": "\n",
            "trailing newline": KNOWN_ALL + "\n",
            "leading space": " " + KNOWN_ALL,
            "inner space": KNOWN_ALL[:10] + " " + KNOWN_ALL[10:],
            "NUL": "\x00",
            "NUL inside": KNOWN_ALL[:10] + "\x00" + KNOWN_ALL[10:],
            "padding": KNOWN_ALL + "=",
            "standard alphabet plus": KNOWN_ALL[:5] + "+" + KNOWN_ALL[6:],
            "standard alphabet slash": KNOWN_ALL[:5] + "/" + KNOWN_ALL[6:],
            "dot": KNOWN_ALL[:5] + "." + KNOWN_ALL[6:],
            "full width letter": "Ａ" * 8,
            "emoji": "\U0001f600" * 8,
            "surrogate": "\ud800" * 8,
            "arabic digit": "١" * 8,
            "percent escape": "%41" * 8,
            "quote": "'" * 8,
            "SQL": "'; DROP TABLE projects; --",
            "path": "../../etc/passwd",
            "one character too many (length % 4 == 1)": KNOWN_ALL + "A",
        }.items():
            with self.subTest(name):
                self.assertRefused(bad, InputProblem.INVALID_CURSOR)

    def test_base64_that_is_not_the_canonical_spelling_is_refused(self):
        # The last character carries two bits that the decoder drops: a different
        # character with the same decoded bytes is a second spelling of one cursor.
        self.assertEqual(len(KNOWN_ALL) % 4, 3)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        head, last = KNOWN_ALL[:-1], KNOWN_ALL[-1]
        siblings = [
            char
            for char in alphabet
            if char != last
            and base64.urlsafe_b64decode(head + char + "=")
            == base64.urlsafe_b64decode(KNOWN_ALL + "=")
        ]
        self.assertTrue(siblings)
        for char in siblings:
            with self.subTest(char=char):
                self.assertRefused(head + char, InputProblem.INVALID_CURSOR)

    def test_a_decoded_text_of_the_wrong_shape_is_refused(self):
        u = str(ID)
        for name, decoded in {
            "other version": token(version="2"),
            "no version": f"all.1790251200123456.{u}",
            "unknown filter": token("deleted"),
            "upper case filter": token("ALL"),
            "empty filter": token(""),
            "filter with a suffix": token("allx"),
            "missing part": "1.all.1790251200123456",
            "extra part": token() + ".x",
            "empty text": "",
            "only separators": "...",
            "leading zero micros": token(micros="01790251200123456"),
            "minus zero": token(micros="-0"),
            "plus sign": token(micros="+5"),
            "space in micros": token(micros=" 5"),
            "decimal point": token(micros="5.0"),
            "exponent": token(micros="1e5"),
            "hex micros": token(micros="0x10"),
            "empty micros": token(micros=""),
            "19 digit micros": token(micros="1" * 19),
            "arabic digit micros": token(micros="١٢"),
            "one past the largest instant": token(micros=MAX_MICROS + 1),
            "one before the smallest instant": token(micros=MIN_MICROS - 1),
            "huge instant": token(micros=10**17 * 9),
            "upper case id": token(ident=u.upper()),
            "id without hyphens": token(ident=u.replace("-", "")),
            "id in braces": token(ident="{" + u + "}"),
            "id as urn": token(ident="urn:uuid:" + u),
            "short id": token(ident=u[:-1]),
            "long id": token(ident=u + "0"),
            "not hex": token(ident="g" + u[1:]),
            "id with newline": token() + "\n",
            "leading newline": "\n" + token(),
            "SQL in the id": token(ident="' OR '1'='1"),
            "a decoded NUL": token() + "\x00",
        }.items():
            with self.subTest(name):
                self.assertRefused(b64(decoded), InputProblem.INVALID_CURSOR)

    def test_a_decoded_text_that_is_not_ascii_is_refused(self):
        raw = base64.urlsafe_b64encode("1.all.1790251200123456.é".encode()).decode()
        self.assertRefused(raw.rstrip("="), InputProblem.INVALID_CURSOR)
        binary = base64.urlsafe_b64encode(bytes(range(200, 240))).decode()
        self.assertRefused(binary.rstrip("="), InputProblem.INVALID_CURSOR)

    def test_a_cursor_belongs_to_the_list_it_came_from(self):
        for issued in FILTERS:
            cursor = encode_cursor(issued, AT, ID)
            for asked in FILTERS:
                with self.subTest(issued=issued, asked=asked):
                    if issued is asked:
                        self.assertEqual(decode_cursor(cursor, asked), Keyset(AT, ID))
                    else:
                        self.assertRefused(cursor, InputProblem.INVALID_CURSOR, asked)

    def test_the_error_names_only_the_field_and_a_closed_problem(self):
        with self.assertRaises(InvalidProjectInputError) as raised:
            decode_cursor(b64("secret-" + "x" * 30), None)
        self.assertEqual(str(raised.exception), "Invalid cursor: invalid_cursor")

    def test_hostile_text_never_raises_anything_but_the_typed_error(self):
        rng = random.Random(84)
        alphabets = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=+/.",
            "0123456789.-abcdef",
            "\x00\n\r\t '\";%é١\ud800\U0001f600",
        )
        valid = [encode_cursor(status, AT, ID) for status in FILTERS]
        candidates: list[str] = []
        for _ in range(4000):
            alphabet = rng.choice(alphabets)
            candidates.append(
                "".join(
                    rng.choice(alphabet)
                    for _ in range(rng.randint(0, MAX_CURSOR_CHARS + 5))
                )
            )
        for _ in range(4000):  # a valid cursor with one thing done to it
            text = rng.choice(valid)
            position = rng.randrange(len(text))
            match rng.randrange(4):
                case 0:
                    text = (
                        text[:position]
                        + rng.choice(alphabets[1])
                        + text[position + 1 :]
                    )
                case 1:
                    text = text[:position]
                case 2:
                    text = text[:position] + rng.choice(alphabets[3]) + text[position:]
                case _:
                    text = text[:position] + text[position + 1 :]
            candidates.append(text)
        accepted = 0
        for candidate in candidates:
            for status in FILTERS:
                try:
                    keyset = decode_cursor(candidate, status)
                except InvalidProjectInputError as error:
                    self.assertEqual(error.field, "cursor")
                    self.assertIn(
                        error.problem,
                        (
                            InputProblem.INVALID_CURSOR,
                            InputProblem.TOO_LONG,
                        ),
                    )
                else:
                    accepted += 1
                    # Whatever is accepted is exactly what encode_cursor writes.
                    self.assertEqual(
                        encode_cursor(status, keyset.created_at, keyset.id), candidate
                    )
        # The generator does reach the valid cursors (a mutation that changed
        # nothing), so "never raised anything else" is not vacuous.
        self.assertGreater(accepted, 0)


if __name__ == "__main__":
    unittest.main()
