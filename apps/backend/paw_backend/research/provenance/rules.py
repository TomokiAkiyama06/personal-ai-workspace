"""Pure rules of the provenance store (PAW-052): no database, no clock, no I/O.

Five small functions. Each is a stub with an exact contract; the tests in
``tests/test_provenance_rules.py`` pin every rule with concrete values.
"""

from collections.abc import Sequence
from uuid import UUID

from paw_backend.research.provenance.records import (
    Claim,
    EntityKind,
    Relation,
    SourceLink,
    SourceLinkInput,
    TracedClaim,
)


def normalize_claim_text(text: str) -> str:
    """The form of a claim text that decides whether two texts are "the same".

    Steps, in this order:

    1. ``unicodedata.normalize("NFKC", text)``;
    2. ``str.casefold()``;
    3. ``unicodedata.normalize("NFKC", ...)`` again (case folding can produce
       characters that NFKC changes; this makes the function idempotent);
    4. collapse every run of whitespace to one ``" "`` and remove leading and
       trailing whitespace, exactly ``" ".join(value.split())``.

    Nothing else changes: punctuation, digits, word order and accents stay.

    ``text`` that is not a ``str`` raises ``TypeError``. The empty string (and a
    whitespace-only one) gives ``""``. Examples::

        normalize_claim_text("  The  Sky\\tis BLUE. ")  == "the sky is blue."
        normalize_claim_text("Ｒｕｓｔ　１.７５")          == "rust 1.75"
        normalize_claim_text("Straße")                    == "strasse"
        normalize_claim_text("ﬁne")                       == "fine"
        normalize_claim_text("Café")                      == "café"
        normalize_claim_text("a\\u00a0b\\r\\nc")            == "a b c"
        normalize_claim_text("x") == normalize_claim_text(normalize_claim_text("X"))
    """
    raise NotImplementedError("PAW-052 stub")


def claim_fingerprint(text: str) -> str:
    """The deduplication key of a claim text: the lowercase hexadecimal SHA-256
    (64 characters, no prefix) of ``normalize_claim_text(text)`` encoded as UTF-8.

    Two texts that differ only in case, in whitespace or in a compatibility form
    (full-width letters, ligatures) have the same fingerprint; any other
    difference gives another one. ``text`` that is not a ``str`` raises
    ``TypeError``. Examples::

        claim_fingerprint("") ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        claim_fingerprint(" \\n ") == claim_fingerprint("")
        claim_fingerprint("abc") ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        claim_fingerprint("ABC  ") == claim_fingerprint("abc")
    """
    raise NotImplementedError("PAW-052 stub")


def order_pair(first: UUID, second: UUID) -> tuple[UUID, UUID]:
    """Return ``(low, high)``: the two ids ordered by their 128-bit integer
    value (``UUID.int``), which is also how PostgreSQL orders ``uuid`` values.

    A relation is symmetric, so it is stored once under this order.
    ``order_pair(a, b) == order_pair(b, a)``. Either argument that is not a
    ``UUID`` raises ``TypeError``; equal ids raise ``ValueError`` (a thing is
    not related to itself). The messages are fixed and do not contain the ids.
    Example::

        one = UUID(int=1); two = UUID(int=2)
        order_pair(two, one) == (one, two)
    """
    raise NotImplementedError("PAW-052 stub")


def merge_duplicate_sources(
    links: Sequence[SourceLinkInput],
) -> tuple[SourceLinkInput, ...]:
    """Merge the entries of one ``record_claim`` call that name the same source.

    Two entries name the same source when ``(source.locator,
    source.content_hash)`` is equal (locators are already canonical). The same
    locator with another ``content_hash`` is another source.

    * The result keeps the first entry of each source, in the order the sources
      first appear. Later entries of the same source are dropped whole: their
      ``fetched_at``, ``published_at``, ``source_type`` and ``title`` are
      ignored (the first entry wins).
    * If a later entry of the same source has another ``stance`` than the first,
      raise ``InvalidProvenanceInputError("sources", InputProblem.CONFLICT)``
      (nothing is returned). The same stance is not a conflict.
    * ``links`` is a list or tuple of ``SourceLinkInput`` (already validated by
      the caller). An empty sequence gives ``()``.
    * The input is not modified; the result is a new tuple. The work is linear
      in ``len(links)`` (use a dict, not a nested loop).

    Example: entries ``[A(supports), B(supports), A(supports)]`` give
    ``(A, B)``; ``[A(supports), A(contradicts)]`` raises.
    """
    raise NotImplementedError("PAW-052 stub")


def order_links(links: Sequence[SourceLink]) -> tuple[SourceLink, ...]:
    """Sort the source links of ONE claim for display.

    Order by, in this priority: (1) ``stance``: ``SUPPORTS`` before
    ``CONTRADICTS``; (2) ``source.fetched_at``: newest first (compare instants);
    (3) ``source.id``: ascending by ``UUID.int``. Every link is kept (no
    deduplication). Returns a new tuple; the input is not modified.

    Example: links with (stance, fetched_at) ``(contradicts, 10:00)``,
    ``(supports, 09:00)``, ``(supports, 11:00)`` come out as
    ``(supports, 11:00)``, ``(supports, 09:00)``, ``(contradicts, 10:00)``.
    """
    raise NotImplementedError("PAW-052 stub")


def order_relations(
    entity: EntityKind, entity_id: UUID, relations: Sequence[Relation]
) -> tuple[Relation, ...]:
    """The relations that involve the claim or source ``entity_id``, in display order.

    * Keep a relation only when ``relation.entity is entity`` and ``entity_id``
      is its ``low_id`` or its ``high_id``.
    * Two relations with the same ``(low_id, high_id)`` count once (the first
      wins).
    * Sort by ``(relation.kind.value, other.int)`` where ``other`` is the other
      endpoint (``relation.other(entity_id)``): ``"contradiction"`` sorts before
      ``"duplicate"``, then ascending by the other endpoint's ``UUID.int``.
    * Returns a new tuple; the input is not modified. Nothing matching gives
      ``()``.
    """
    raise NotImplementedError("PAW-052 stub")


def assemble_traced_claims(
    claims: Sequence[Claim],
    links: Sequence[SourceLink],
    relations: Sequence[Relation],
) -> tuple[TracedClaim, ...]:
    """Group flat query results into one :class:`TracedClaim` per claim.

    * The result has one ``TracedClaim`` per claim, in the order of ``claims``.
      A claim whose id already appeared earlier in ``claims`` is skipped.
    * ``TracedClaim.links``: the entries of ``links`` whose ``claim_id`` equals
      the claim's id, ordered by ``order_links``. Two links with the same
      ``(claim_id, source.id)`` count once (the first wins). Links of a claim
      that is not in ``claims`` are ignored.
    * ``TracedClaim.relations``: ``order_relations(EntityKind.CLAIM, claim.id,
      relations)``.
    * A claim without links or relations gets empty tuples.
    * Inputs are not modified. Empty ``claims`` gives ``()``.
    """
    raise NotImplementedError("PAW-052 stub")
