"""Full-text index for Hybrid Retrieval (PAW-043).

One index, no table: ``ix_memory_versions_search``, a GIN index over the
full-text document of an **active** version (``title`` and ``content``). It serves
the keyword leg of ``paw_backend.memory.retrieval``, which filters by permission
(scope, project, repository, status, freshness) in the same statement; the index
only finds the text matches, it never decides who may read a row.

The document is ``to_tsvector('simple', ...)`` of the NFKC-normalised text (its first
100,000 characters: a tsvector over 1 MB would make the INSERT of a very large memory
fail inside the index) with a
blank put around every Hiragana, Katakana and CJK ideograph character: the
``simple`` configuration would otherwise keep a Japanese sentence as one token.
The expression is repeated here on purpose (a migration is a frozen snapshot of
``paw_backend.memory.fulltext.search_document_sql``);
``tests/test_retrieval_migration.py`` fails when the two differ.
``normalize(..., NFKC)`` needs a UTF8 database.

There is no ANN (HNSW / IVFFlat) index on ``memory_embeddings``, and this revision
does not add one: an HNSW index needs a fixed dimension per embedding model, the
model is chosen by the PAW-019 benchmark, and an exact scan restricted by the
permission filter is correct at any size (see Decision 0019 for what an ANN index
must keep). No table is created, so there is no privilege to grant: the retrieval
only reads tables whose privileges revisions 0026 and 0040 already give.

The definition of the index repeats the one in ``paw_backend.memory.models``;
Alembic's autogenerate shows no difference (``tests/test_retrieval_migration.py``).

Revision ID: 0043
Revises: 0026
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_memory_versions_search"
# Frozen copy of ``paw_backend.memory.fulltext.search_document_sql()``.
SEARCH_DOCUMENT = (
    "to_tsvector('simple'::regconfig, regexp_replace("
    "left(NORMALIZE((title || ' '::text) || content, NFKC), 100000),"
    " '([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff])'::text, ' \\1 '::text, 'g'::text))"
)


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "memory_versions",
        [sa.text(SEARCH_DOCUMENT)],
        postgresql_using="gin",
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memory_versions")
