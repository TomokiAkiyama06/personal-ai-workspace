"""``published_utc`` converts with ONE reading of the provider's UTC offset.

The provider's ``tzinfo`` is untrusted code. ``published_utc`` used to read its
``utcoffset()`` to check that the time is aware and then let ``astimezone`` read
it again to convert, so a stateful ``tzinfo`` (offsets ``+01:00`` then ``+02:00``)
turned ``2026-01-01 12:00`` into ``10:00`` UTC and silently corrupted the source's
provenance instead of being classified as a malformed response (issue #90,
finding on #76). Two readings that disagree are now an
``InvalidProviderResponseError``, and the conversion uses the reading that was
checked.
"""

import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from paw_backend.research.providers import (
    InvalidProviderResponseError,
    ProviderKind,
    normalize_hits,
)
from paw_backend.research.providers.normalize import published_utc

from .research_support import NOW, hit

MOMENT = (2026, 1, 1, 12, 0)


def zone_answering(*offsets: timedelta | None) -> tzinfo:
    """A ``tzinfo`` whose n-th ``utcoffset()`` call answers ``offsets[n]``.

    The last answer is repeated once the list is used up; ``calls`` counts them.
    """

    class Stateful(tzinfo):
        calls = 0

        def utcoffset(self, moment):
            answer = offsets[min(self.calls, len(offsets) - 1)]
            self.calls += 1
            return answer

        def dst(self, moment):
            return None

        def tzname(self, moment):
            return None

    return Stateful()


class OneOffsetSnapshotTest(unittest.TestCase):
    def test_offsets_that_change_between_readings_are_an_invalid_response(self):
        cases = {
            "+01:00 then +02:00": (timedelta(hours=1), timedelta(hours=2)),
            "+01:00 then UTC": (timedelta(hours=1), timedelta(0)),
            "UTC then -05:00": (timedelta(0), timedelta(hours=-5)),
            "an offset, then naive": (timedelta(hours=1), None),
        }
        for name, offsets in cases.items():
            with self.subTest(name):
                zone = zone_answering(*offsets)
                with self.assertRaises(InvalidProviderResponseError):
                    published_utc(datetime(*MOMENT, tzinfo=zone))

    def test_a_hit_with_such_a_timezone_is_rejected_as_a_whole(self):
        bad = hit("https://a.example/bad")
        zone = zone_answering(timedelta(hours=1), timedelta(hours=2))
        object.__setattr__(bad, "published_at", datetime(*MOMENT, tzinfo=zone))
        with self.assertRaises(InvalidProviderResponseError):
            normalize_hits(
                provider_id="web-a",
                kind=ProviderKind.WEB,
                hits=[hit("https://a.example/ok"), bad],
                limit=10,
                retrieved_at=NOW,
            )

    def test_a_steady_timezone_still_converts_with_that_offset(self):
        for name, offset, expected in (
            ("+01:00", timedelta(hours=1), datetime(2026, 1, 1, 11, 0, tzinfo=UTC)),
            ("UTC", timedelta(0), datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
            (
                "-05:30",
                timedelta(hours=-5, minutes=-30),
                datetime(2026, 1, 1, 17, 30, tzinfo=UTC),
            ),
        ):
            with self.subTest(name):
                converted = published_utc(
                    datetime(*MOMENT, tzinfo=zone_answering(offset))
                )
                self.assertEqual(converted, expected)
                self.assertIs(type(converted), datetime)
                self.assertIs(converted.tzinfo, UTC)

    def test_a_standard_timezone_and_none_are_unchanged(self):
        stamp = datetime(*MOMENT, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(published_utc(stamp), datetime(2026, 1, 1, 3, 0, tzinfo=UTC))
        self.assertIsNone(published_utc(None))
        with self.assertRaises(InvalidProviderResponseError):
            published_utc(datetime(*MOMENT))  # naive

    def test_the_edges_of_the_calendar_are_still_an_invalid_response(self):
        far = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        with self.assertRaises(InvalidProviderResponseError):
            published_utc(far)
        early = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        with self.assertRaises(InvalidProviderResponseError):
            published_utc(early)


if __name__ == "__main__":
    unittest.main()
