"""The pgvector ``vector`` column type, written against SQLAlchemy only.

``pgvector-python`` is deliberately not a dependency: this type is all the
schema needs. The column has **no fixed dimension** because the embedding
model (and so its dimension) is chosen by the PAW-019 benchmark, not yet.
Values travel as pgvector's text form (``[0.1,0.2]``), which needs no driver
support, and come back as ``list[float]``.

Distances are only defined between vectors of the same dimension, so a query
must restrict ``memory_embeddings`` to one ``embedding_model_id`` first.
"""

import math
from collections.abc import Sequence

from sqlalchemy import Float, cast
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType


def format_vector(values: Sequence[float]) -> str:
    """Render ``values`` in pgvector's text form; reject anything else."""
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise TypeError("a vector must be a sequence of numbers")
    if not values:
        raise ValueError("a vector needs at least one dimension")
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("a vector must be a sequence of numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        numbers.append(number)
    return "[" + ",".join(repr(number) for number in numbers) + "]"


def parse_vector(text: str) -> list[float]:
    """Parse pgvector's text form back into a list of floats."""
    return [float(part) for part in text.strip()[1:-1].split(",")]


class Vector(UserDefinedType[list[float]]):
    """``vector``; without ``dimensions`` pgvector accepts any dimension.

    The schema uses no dimension. ``dimensions`` exists so that reflecting a
    ``vector(N)`` column (SQLAlchemy passes the ``N``) keeps working once a later
    migration, such as PAW-043's per-model index, introduces a typed vector.
    """

    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:
        return "vector" if self.dimensions is None else f"vector({self.dimensions})"

    def bind_processor(self, dialect):
        def process(value):
            return None if value is None else format_vector(value)

        return process

    def bind_expression(self, bindvalue):
        # The driver sends the text form; the server needs to know it is a vector.
        return cast(bindvalue, self)

    def result_processor(self, dialect, coltype):
        def process(value):
            return None if value is None else parse_vector(value)

        return process

    class comparator_factory(UserDefinedType.Comparator):
        def l2_distance(self, other):
            return self.op("<->", return_type=Float)(other)

        def cosine_distance(self, other):
            return self.op("<=>", return_type=Float)(other)

        def negative_inner_product(self, other):
            return self.op("<#>", return_type=Float)(other)


# Lets reflection (Alembic's drift check) recognise the column type.
ischema_names["vector"] = Vector
