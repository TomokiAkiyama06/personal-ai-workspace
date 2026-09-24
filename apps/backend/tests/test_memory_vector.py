"""pgvector: the column type (no server needed) and storage / search (PostgreSQL)."""

import unittest
from math import inf, nan
from uuid import uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.exc import DataError
from sqlalchemy.schema import CreateTable

from paw_backend.memory.models import MemoryEmbedding, MemoryVersion
from paw_backend.memory.vector import Vector, format_vector, parse_vector

from .memory_support import MemoryDatabaseTestCase, requires_postgres


class VectorTypeTest(unittest.TestCase):
    def test_a_vector_is_rendered_in_pgvector_text_form(self):
        self.assertEqual(format_vector([1, 2.5, -0.125]), "[1.0,2.5,-0.125]")
        self.assertEqual(format_vector((0.1,)), "[0.1]")

    def test_the_text_form_is_parsed_back(self):
        self.assertEqual(parse_vector("[1,2.5,-0.125]"), [1.0, 2.5, -0.125])
        self.assertEqual(parse_vector(format_vector([1e-05, 3.0])), [1e-05, 3.0])

    def test_invalid_vectors_are_refused_before_reaching_the_database(self):
        cases = [
            ([], ValueError),
            ([nan], ValueError),
            ([1.0, inf], ValueError),
            ("[1,2]", TypeError),
            (b"[1,2]", TypeError),
            ([1, "2"], TypeError),
            ([True, 1.0], TypeError),
            (None, TypeError),
            (5, TypeError),
        ]
        for value, error in cases:
            with self.subTest(value=value), self.assertRaises(error):
                format_vector(value)

    def test_the_column_has_no_fixed_dimension(self):
        ddl = str(
            CreateTable(MemoryEmbedding.__table__).compile(dialect=postgresql.dialect())
        )

        self.assertIn("embedding vector NOT NULL", ddl)
        self.assertNotIn("vector(", ddl)

    def test_a_dimension_is_optional_and_only_rendered_when_given(self):
        dialect = postgresql.dialect()

        self.assertEqual(Vector().get_col_spec(), "vector")
        self.assertEqual(Vector(3).get_col_spec(), "vector(3)")
        # Reflection hands over the typmod of a ``vector(N)`` column.
        self.assertEqual(ischema_names["vector"](768).dimensions, 768)
        self.assertEqual(Vector(3).compile(dialect=dialect), "vector(3)")

    def test_a_bound_vector_is_cast_to_the_vector_type(self):
        statement = select(MemoryEmbedding.memory_version_id).where(
            MemoryEmbedding.embedding.cosine_distance([1.0, 0.0]) < 0.5
        )

        sql = str(statement.compile(dialect=postgresql.dialect()))

        self.assertIn("memory_embeddings.embedding <=> CAST(", sql)
        self.assertIn("AS vector)", sql)

    def test_the_type_is_recognised_when_the_database_is_reflected(self):
        self.assertIs(ischema_names["vector"], Vector)


@requires_postgres
class PgvectorTest(MemoryDatabaseTestCase):
    def add_embedding(self, version, vector, model="model-a", dimensions=None):
        return self.session.execute(
            insert(MemoryEmbedding).values(
                memory_version_id=version,
                embedding_model_id=model,
                dimensions=len(vector) if dimensions is None else dimensions,
                embedding=vector,
            )
        )

    def test_the_extension_is_available(self):
        installed = self.connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        self.assertEqual(installed, "vector")

    def test_a_vector_can_be_stored_and_read_back(self):
        version = self.add_version(self.add_memory())
        self.add_embedding(version, [0.25, -1.5, 3.0])

        stored = self.session.execute(
            select(MemoryEmbedding.embedding, MemoryEmbedding.dimensions)
        ).one()

        self.assertEqual(tuple(stored), ([0.25, -1.5, 3.0], 3))

    def test_a_nearest_neighbour_query_returns_rows_closest_first(self):
        versions = {
            name: self.add_version(self.add_memory(), title=name)
            for name in ("x-axis", "y-axis", "diagonal", "far")
        }
        vectors = {
            "x-axis": [1.0, 0.0],
            "y-axis": [0.0, 1.0],
            "diagonal": [0.7, 0.7],
            "far": [-5.0, -5.0],
        }
        for name, vector in vectors.items():
            self.add_embedding(versions[name], vector)

        def ranked(distance) -> list[str]:
            rows = self.session.execute(
                select(MemoryVersion.title)
                .join(
                    MemoryEmbedding,
                    MemoryEmbedding.memory_version_id == MemoryVersion.id,
                )
                .where(MemoryEmbedding.embedding_model_id == "model-a")
                .order_by(distance)
            )
            return list(rows.scalars())

        query = [0.9, 0.1]
        self.assertEqual(
            ranked(MemoryEmbedding.embedding.l2_distance(query)),
            ["x-axis", "diagonal", "y-axis", "far"],
        )
        self.assertEqual(
            ranked(MemoryEmbedding.embedding.cosine_distance(query)),
            ["x-axis", "diagonal", "y-axis", "far"],
        )
        # Inner product: pgvector returns the negated value, so ascending
        # order still means "most similar first".
        self.assertEqual(
            ranked(MemoryEmbedding.embedding.negative_inner_product(query)),
            ["x-axis", "diagonal", "y-axis", "far"],
        )

    def test_the_distance_value_is_computed_by_the_database(self):
        version = self.add_version(self.add_memory())
        self.add_embedding(version, [3.0, 4.0])

        distance = self.session.execute(
            select(MemoryEmbedding.embedding.l2_distance([0.0, 0.0]))
        ).scalar_one()

        self.assertEqual(distance, 5.0)

    def test_models_of_different_dimensions_coexist_when_queried_per_model(self):
        version = self.add_version(self.add_memory())
        self.add_embedding(version, [1.0, 2.0, 3.0], model="small")
        self.add_embedding(version, [0.5] * 8, model="large")

        small = self.session.execute(
            select(MemoryEmbedding.embedding.l2_distance([1.0, 2.0, 3.0])).where(
                MemoryEmbedding.embedding_model_id == "small"
            )
        ).scalar_one()
        large = self.session.execute(
            select(MemoryEmbedding.dimensions).where(
                MemoryEmbedding.embedding_model_id == "large"
            )
        ).scalar_one()

        self.assertEqual((small, large), (0.0, 8))

    def test_distances_across_dimensions_fail_so_the_model_must_be_filtered_first(self):
        version = self.add_version(self.add_memory())
        self.add_embedding(version, [1.0, 2.0, 3.0], model="small")
        self.add_embedding(version, [0.5] * 8, model="large")

        with self.assertRaises(DataError), self.session.begin_nested():
            self.session.execute(
                select(MemoryEmbedding.embedding.l2_distance([1.0, 2.0, 3.0]))
            ).all()

    def test_dimensions_must_describe_the_stored_vector(self):
        version = self.add_version(self.add_memory())
        cases = {
            "ck_memory_embeddings_dimensions_match": {"dimensions": 4},
            "ck_memory_embeddings_model_id_length": {"model": ""},
        }
        for expected, options in cases.items():
            with self.subTest(expected):
                self.assertEqual(
                    self.violation(
                        lambda options=options: self.add_embedding(
                            version, [1.0, 2.0, 3.0], **options
                        )
                    ),
                    expected,
                )

    def test_one_embedding_per_version_and_model(self):
        version = self.add_version(self.add_memory())
        self.add_embedding(version, [1.0, 2.0])

        clash = self.violation(lambda: self.add_embedding(version, [3.0, 4.0]))
        other_model = self.violation(
            lambda: self.add_embedding(version, [3.0, 4.0], model="model-b")
        )

        self.assertEqual(clash, "pk_memory_embeddings")
        self.assertIsNone(other_model)

    def test_an_embedding_needs_an_existing_version(self):
        self.assertEqual(
            self.violation(lambda: self.add_embedding(uuid4(), [1.0])),
            "fk_memory_embeddings_memory_version_id_memory_versions",
        )

    def test_there_is_no_approximate_index_yet(self):
        # PAW-043 adds it once the embedding model (and dimension) is chosen.
        indexes = self.connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE tablename = 'memory_embeddings' ORDER BY indexname"
            )
        ).all()

        self.assertEqual(
            [name for name, _ in indexes],
            ["ix_memory_embeddings_embedding_model_id", "pk_memory_embeddings"],
        )
        self.assertFalse(any("hnsw" in d or "ivfflat" in d for _, d in indexes))


if __name__ == "__main__":
    unittest.main()
