"""What a foreign component may answer with, as numbers (pure, no database).

An Embedder or a Reranker is code this project does not own. Whatever it returns
is checked before it is used, and a value that cannot be a finite number must be
*refused* (the stage degrades), never turned into an exception that fails the whole
retrieval: ``float(10**400)`` and ``math.isfinite(10**400)`` both raise
``OverflowError``, and a class can make ``__float__`` raise anything.
"""

import math
import unittest
from decimal import Decimal
from fractions import Fraction

from paw_backend.memory.retrieval.stages import component_number, unit_vector


class ExplodingInt(int):
    def __float__(self):
        raise RuntimeError("secret-connection-string")


class ExplodingFloat(float):
    def __float__(self):
        raise RuntimeError("secret-connection-string")


class NotANumberButFloat(float):
    def __float__(self):
        return math.nan


HOSTILE = [
    10**400,
    -(10**400),
    10**309,
    True,
    False,
    math.nan,
    math.inf,
    -math.inf,
    Decimal("1e400"),
    Decimal("0.5"),
    Fraction(1, 2),
    "0.5",
    b"1",
    None,
    complex(1, 0),
    object(),
    [1],
    (1,),
    ExplodingInt(1),
    ExplodingFloat(1.0),
    NotANumberButFloat(1.0),
]


class ComponentNumberTest(unittest.TestCase):
    def test_every_hostile_value_is_refused_without_raising(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)[:30]):
                self.assertIsNone(component_number(value))

    def test_ints_and_floats_are_accepted_as_floats(self):
        for value, expected in [
            (0, 0.0),
            (1, 1.0),
            (-3, -3.0),
            (0.25, 0.25),
            (10**300, 1e300),
            (-(10**39), -1e39),
            (1.7976931348623157e308, 1.7976931348623157e308),
            (5e-324, 5e-324),
        ]:
            with self.subTest(value=value):
                result = component_number(value)
                self.assertEqual(result, expected)
                self.assertIs(type(result), float)

    def test_the_largest_int_that_fits_a_float_is_accepted_and_the_next_is_not(self):
        largest = int(1.7976931348623157e308)
        self.assertEqual(component_number(largest), 1.7976931348623157e308)
        self.assertIsNone(component_number(largest * 10))


class UnitVectorTest(unittest.TestCase):
    def test_a_vector_is_scaled_to_length_one_without_changing_its_direction(self):
        self.assertEqual(unit_vector([3.0, 4.0]), [0.6, 0.8])
        self.assertEqual(unit_vector([0.0, -2.0, 0.0]), [0.0, -1.0, 0.0])

    def test_values_beyond_the_range_of_a_float32_are_brought_into_range(self):
        # pgvector stores float4: 1e39 is refused by the database, 1e30 squared
        # overflows inside its distance. The direction is all a cosine uses.
        for vector in (
            [3e39, 4e39],
            [3e300, 4e300],
            [1.7e308, 1.7e308],
            [3e-300, 4e-300],
        ):
            with self.subTest(vector=vector):
                unit = unit_vector(vector)
                self.assertAlmostEqual(math.hypot(*unit), 1.0)
                self.assertTrue(all(abs(x) <= 1.0 for x in unit))
                self.assertAlmostEqual(unit[1] / unit[0], vector[1] / vector[0])

    def test_a_tiny_component_beside_a_huge_one_becomes_zero_not_an_error(self):
        self.assertEqual(unit_vector([1e300, 1e-300]), [1.0, 0.0])

    def test_denormal_only_vectors_still_have_a_direction(self):
        unit = unit_vector([5e-324, 5e-324])
        self.assertAlmostEqual(unit[0], 2**-0.5)

    def test_the_zero_vector_has_no_direction(self):
        self.assertIsNone(unit_vector([0.0, 0.0, 0.0]))
        self.assertIsNone(unit_vector([]))


if __name__ == "__main__":
    unittest.main()
