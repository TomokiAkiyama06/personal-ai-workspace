"""Words and character pairs: what the keyword leg and the fakes see in a text.

This module is outside ``retrieval/`` because the model of ``memory_versions``
needs :func:`search_document_sql` for its full-text index, and importing the
retrieval package from the model would be circular.

The keyword leg is PostgreSQL full-text search with the ``simple`` configuration
(no language, no stop words). ``simple`` cuts text at blanks and punctuation,
which leaves a Japanese sentence as ONE token, so a query would only ever match
a whole sentence. Without an extension that knows Japanese (MeCab based, pg_bigm,
PGroonga: none is installed), the search document is therefore made by putting a
blank around every character of the Hiragana, Katakana and CJK ideograph blocks
(:data:`CJK_CLASS`), and the query side asks for **pairs of neighbouring
characters** as phrases (``'検' <-> '索'``), which the full-text index answers
from the token positions. Other text is matched word by word.

The same normalization is applied on both sides: Unicode NFKC (full-width
letters and digits, half-width Katakana) and lower case. This is a compromise,
not morphological analysis: it finds text that contains the same characters in
the same order and ranks by how many pairs match. Decision 0019 lists the
alternatives.

These are pure functions with no I/O. :func:`search_document_sql` is the ONE
place that spells the SQL expression: the migration, the model's index and every
query use it, so the index and the queries cannot drift apart.
"""

import re
import unicodedata

# Bounds of a keyword query (see ``retrieval/limits.py``, which re-exports them).
MAX_QUERY_TERMS = 64
MAX_TERM_CHARS = 64

# Hiragana and Katakana (U+3040-30FF, with the prolonged sound mark), CJK
# Extension A (U+3400-4DBF) and the CJK Unified Ideographs (U+4E00-9FFF). Half
# width Katakana is folded into the Katakana block by NFKC first.
CJK_CLASS = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"

_CJK_CHARACTER = re.compile(f"[{CJK_CLASS}]")
_RUNS = re.compile(f"[{CJK_CLASS}]+|[^{CJK_CLASS}]+")
_WORD = re.compile(r"\w+")
_CJK_ONLY = re.compile(f"[{CJK_CLASS}]+")


def normalize_text(text: str) -> str:
    """NFKC and lower case: the form both the index and the query are compared in."""
    return unicodedata.normalize("NFKC", text).lower()


def features(text: str) -> tuple[str, ...]:
    """The words and Japanese character pairs of ``text``, in order, with repeats.

    A run of the CJK blocks with two or more characters gives its overlapping
    pairs (``"検索する"`` is ``検索``, ``索す``, ``する``), a single one gives itself;
    any other run of word characters is one word. Tokens without a letter or
    digit (``"_"``) are dropped.
    """
    found: list[str] = []
    for word in _WORD.findall(normalize_text(text)):
        for run in _RUNS.findall(word):
            if _CJK_ONLY.fullmatch(run):
                if len(run) == 1:
                    found.append(run)
                else:
                    found.extend(run[i : i + 2] for i in range(len(run) - 1))
            elif any(character.isalnum() for character in run):
                found.append(run)
    return tuple(found)


def keyword_terms(text: str) -> tuple[str, ...]:
    """The distinct features of ``text`` a keyword query asks for, bounded.

    Order of first appearance is kept; a feature longer than ``MAX_TERM_CHARS``
    is dropped and at most ``MAX_QUERY_TERMS`` remain.
    """
    seen: dict[str, None] = {}
    for feature in features(text):
        if len(feature) <= MAX_TERM_CHARS:
            seen.setdefault(feature)
        if len(seen) >= MAX_QUERY_TERMS:
            break
    return tuple(seen)


def tsquery_text(terms: tuple[str, ...]) -> str | None:
    """The ``to_tsquery('simple', ...)`` text that matches ANY of ``terms``.

    ``None`` when there is no term. A word is one quoted lexeme, a pair of CJK
    characters is a phrase of the two, terms are joined with ``|``. Only word
    characters ever reach the string (:func:`features` produced them), so there
    is nothing to escape and no tsquery operator can come from the caller's text;
    a term that still contains anything else is dropped rather than quoted.
    """
    parts: list[str] = []
    for term in terms:
        if _WORD.fullmatch(term) is None:
            continue
        if _CJK_ONLY.fullmatch(term) and len(term) == 2:
            parts.append(f"'{term[0]}' <-> '{term[1]}'")
        else:
            parts.append(f"'{term}'")
    return " | ".join(parts) if parts else None


def search_document_sql(title: str = "title", content: str = "content") -> str:
    """The full-text document of a version, as a SQL expression over two columns.

    ``title`` and ``content`` are SQL column references (never caller text). The
    expression is IMMUTABLE, so it can be indexed; with the default arguments it
    is exactly the one of the index ``ix_memory_versions_search``.

    It is written the way PostgreSQL prints the index definition (in the pretty
    form of ``pg_get_indexdef``: the parentheses it keeps and the ``::text`` casts
    it adds). Alembic's autogenerate compares an index expression as text (after
    dropping casts, quotes and blanks) and would otherwise report a difference
    between the model and the database that is not one.
    """
    return (
        "to_tsvector('simple'::regconfig, regexp_replace("
        f"NORMALIZE(({title} || ' '::text) || {content}, NFKC),"
        f" '([{CJK_CLASS}])'::text, ' \\1 '::text, 'g'::text))"
    )
