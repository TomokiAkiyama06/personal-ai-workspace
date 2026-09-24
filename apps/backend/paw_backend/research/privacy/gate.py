"""``PrivacyGate``: what may be sent to an external search, and the audit of it.

The gate decides; the small pure functions in ``rules.py`` do the text work. This
module keeps everything that must not depend on those functions being right: the
validation of the caller's input, the default-deny handling of unclassified
context, the fixed order of the steps, the independent safety checks on the
finished query, and "audit first, then send".

Order of ``PrivacyGate.minimize`` (the draft is the caller's proposed query):

1. Refuse an unclassified context, a draft over ``MAX_DRAFT_CHARS`` and a context
   over the limits, before any text work.
2. ``normalize_text`` the draft.
3. For every non-public piece (in the order given): ``normalize_text`` its text
   and find the spans of the draft copied from it with ``find_copied_spans``
   (window ``copy_window(label)``, shortened to the length of the piece, both
   counted in case-folded characters: matching uses full Unicode case folding,
   so ``ß`` and ``SS`` are the same). All spans are merged and replaced by spaces
   in one go.
4. ``strip_credentials``.
5. The abstraction rules, in this order: ``abstract_urls``, ``abstract_emails``,
   ``abstract_paths``, ``abstract_hosts``, ``abstract_ids``,
   ``drop_opaque_tokens``, ``generalize_versions``.
6. ``truncate_query`` to ``MAX_MINIMIZED_QUERY_CHARS``.
7. Safety checks that do not use ``rules.py``: the query has a word character;
   ``redact_text`` finds no credential in it; no whole non-public piece and no
   word of 4 or more characters of a secret is in it (compared after NFKC,
   removal of format characters and full case folding with ``str.casefold``,
   the same folding as the copy detection but not taken from ``rules.py``).

Nothing in an exception, a log line or a record contains the draft, a piece, the
query or an exception text of the audit sink.
"""

import asyncio
import inspect
import logging
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from paw_backend.research.privacy import rules
from paw_backend.research.privacy.contract import (
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    MAX_AUDIT_TIMEOUT_SECONDS,
    MAX_CONTEXT_PIECES,
    MAX_DRAFT_CHARS,
    MAX_MINIMIZED_QUERY_CHARS,
    MAX_TOTAL_CONTEXT_CHARS,
    SECRET_WINDOW_CHARS,
    ContextLabel,
    ContextPiece,
    ExternalSendAudit,
    ExternalSendRecord,
    MinimizedQuery,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    WithheldCounts,
    copy_window,
)
from paw_backend.research.providers.contract import (
    KIND_ORDER,
    ProviderKind,
    ResearchRequest,
)
from paw_backend.tools.credentials import redact_text

logger = logging.getLogger(__name__)

# The abstraction rules of ``rules.py`` in the order they run (see the module
# docstring), looked up by name at call time.
ABSTRACTION_RULE_NAMES: tuple[str, ...] = (
    "abstract_urls",
    "abstract_emails",
    "abstract_paths",
    "abstract_hosts",
    "abstract_ids",
    "drop_opaque_tokens",
    "generalize_versions",
)


def merge_spans(spans: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Sort ``(start, end)`` pairs and merge those that touch or overlap."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def replace_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` with each span (sorted, disjoint, inside it) replaced by a space."""
    parts: list[str] = []
    position = 0
    for start, end in spans:
        if not position <= start < end <= len(text):
            raise ValueError("spans must be sorted, disjoint and inside the text")
        parts.append(text[position:start])
        parts.append(" ")
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _guard_key(text: str) -> str:
    """The comparison form of the safety checks: independent of ``rules.py``.

    Full Unicode case folding (``str.casefold``), like ``rules.fold_for_match``:
    ``ß`` and ``SS``, ``İ`` and ``i`` plus a combining dot, ``ς`` and ``σ`` are
    equal here too."""
    folded = unicodedata.normalize("NFKC", text)
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in folded
        if ch.isspace() or unicodedata.category(ch) not in {"Cc", "Cf"}
    )
    return " ".join(kept.casefold().split())


def _validate_audit(audit: object) -> None:
    record = getattr(audit, "record", None)
    if not inspect.iscoroutinefunction(record):
        raise TypeError("audit must implement ExternalSendAudit")
    try:
        inspect.signature(record).bind(object())
    except (TypeError, ValueError):
        raise TypeError("audit must implement ExternalSendAudit") from None


class PrivacyGate:
    """Minimises a draft query, refuses what cannot be made safe, audits the send.

    ``audit`` must implement ``ExternalSendAudit`` (``async def record(record)``:
    checked here, ``TypeError`` otherwise). ``clock`` returns the current
    time as a timezone-aware datetime (UTC by default), injectable for tests.
    ``audit_timeout_seconds`` (a number greater than 0 and at most
    ``MAX_AUDIT_TIMEOUT_SECONDS``) bounds one ``audit.record`` call.
    """

    def __init__(
        self,
        audit: ExternalSendAudit,
        *,
        clock: Callable[[], datetime] | None = None,
        audit_timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
    ) -> None:
        _validate_audit(audit)
        if isinstance(audit_timeout_seconds, bool) or not isinstance(
            audit_timeout_seconds, int | float
        ):
            raise TypeError("audit_timeout_seconds must be a number")
        if not 0 < audit_timeout_seconds <= MAX_AUDIT_TIMEOUT_SECONDS:
            raise ValueError("audit_timeout_seconds is out of range")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audit_timeout = float(audit_timeout_seconds)

    # -- the decision ----------------------------------------------------------

    def minimize(self, draft: str, context: Sequence[Any]) -> MinimizedQuery:
        """Turn ``draft`` into a query that may leave the backend, or refuse.

        ``draft`` must be a ``str`` and ``context`` a ``list`` or ``tuple``
        (``TypeError`` otherwise); a draft that cannot be encoded as UTF-8 is a
        ``ValueError``. Everything else that goes wrong is a ``PrivacyRefusal``
        with a closed reason:

        * an element of ``context`` that is not a ``ContextPiece``:
          ``UNCLASSIFIED_CONTEXT`` (checked first);
        * ``len(draft) > MAX_DRAFT_CHARS``: ``DRAFT_TOO_LONG``;
        * more than ``MAX_CONTEXT_PIECES`` pieces, or more than
          ``MAX_TOTAL_CONTEXT_CHARS`` characters of text in all:
          ``CONTEXT_TOO_LARGE``;
        * no word character left after the steps in the module docstring:
          ``EMPTY_QUERY``; a credential or non-public text left in the finished
          query: ``CREDENTIAL_REMAINS`` / ``PRIVATE_TEXT_REMAINS``.

        The context may be empty (the abstraction and credential rules still
        apply). PUBLIC pieces are never used to change the draft.
        """
        if not isinstance(draft, str):
            raise TypeError("draft must be a str")
        if not isinstance(context, list | tuple):
            raise TypeError("context must be a list or a tuple")
        try:
            draft.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("draft must be encodable as UTF-8") from None
        if not all(isinstance(piece, ContextPiece) for piece in context):
            raise PrivacyRefusal(RefusalReason.UNCLASSIFIED_CONTEXT)
        if len(draft) > MAX_DRAFT_CHARS:
            raise PrivacyRefusal(RefusalReason.DRAFT_TOO_LONG)
        if (
            len(context) > MAX_CONTEXT_PIECES
            or sum(len(piece.text) for piece in context) > MAX_TOTAL_CONTEXT_CHARS
        ):
            raise PrivacyRefusal(RefusalReason.CONTEXT_TOO_LARGE)

        text = rules.normalize_text(draft)
        text, pieces_matched = self._remove_copied_text(text, context)
        text, credentials_removed = rules.strip_credentials(text)
        abstractions = 0
        for name in ABSTRACTION_RULE_NAMES:
            text, count = getattr(rules, name)(text)
            abstractions += count
        truncated = len(text) > MAX_MINIMIZED_QUERY_CHARS
        text = rules.truncate_query(text, MAX_MINIMIZED_QUERY_CHARS)
        self._check_finished_query(text, context)
        return MinimizedQuery(
            query=text,
            fingerprint=rules.query_fingerprint(text),
            truncated=truncated,
            credentials_removed=credentials_removed,
            pieces_matched=pieces_matched,
            abstractions=abstractions,
            withheld=WithheldCounts.from_pieces(context),
        )

    @staticmethod
    def _remove_copied_text(
        text: str, context: Sequence[ContextPiece]
    ) -> tuple[str, int]:
        """Replace every stretch of ``text`` copied from a non-public piece."""
        spans: list[tuple[int, int]] = []
        matched = 0
        for piece in context:
            if piece.label is ContextLabel.PUBLIC:
                continue
            source = rules.normalize_text(piece.text)
            if not source:
                continue
            # The window counts case-folded characters (a ``ß`` is two of them),
            # so a short piece is compared as a whole, not by its first letters.
            window = min(copy_window(piece.label), len(rules.fold_for_match(source)))
            found = rules.find_copied_spans(text, source, window=window)
            if found:
                matched += 1
                spans.extend(found)
        if not spans:
            return text, matched
        return rules.normalize_text(replace_spans(text, merge_spans(spans))), matched

    @staticmethod
    def _check_finished_query(text: str, context: Sequence[ContextPiece]) -> None:
        """Refuse a finished query that a faulty rule left unsafe (fail closed)."""
        if not any(ch.isalnum() or ch == "_" for ch in text):
            raise PrivacyRefusal(RefusalReason.EMPTY_QUERY)
        if redact_text(text)[1] > 0:
            raise PrivacyRefusal(RefusalReason.CREDENTIAL_REMAINS)
        key = _guard_key(text)
        for piece in context:
            if piece.label is ContextLabel.PUBLIC:
                continue
            source = _guard_key(piece.text)
            if source and source in key:
                raise PrivacyRefusal(RefusalReason.PRIVATE_TEXT_REMAINS)
            if piece.label is ContextLabel.SECRET and any(
                len(word) >= SECRET_WINDOW_CHARS and word in key
                for word in source.split()
            ):
                raise PrivacyRefusal(RefusalReason.PRIVATE_TEXT_REMAINS)

    # -- audit first, then send --------------------------------------------------

    async def authorize(
        self,
        draft: str,
        context: Sequence[Any],
        *,
        project_id: Any,
        provider_kinds: frozenset[ProviderKind],
    ) -> MinimizedQuery:
        """``minimize`` and record the send; return the query only once it is recorded.

        ``project_id`` must be a ``uuid.UUID`` and ``provider_kinds`` a non-empty
        ``frozenset`` of ``ProviderKind`` (``TypeError`` / ``ValueError``). The
        ``ExternalSendRecord`` (the kinds in ``KIND_ORDER`` order, ``recorded_at``
        from the clock in UTC) is passed to ``audit.record``. If the sink raises
        any ``Exception``, or does not finish within ``audit_timeout_seconds``,
        the send is refused with ``PrivacyRefusal(AUDIT_FAILED)``: nothing is sent
        that was not recorded. The failure is logged once at WARNING with the
        exception TYPE name only. ``asyncio.CancelledError`` is never swallowed.
        A refusal from ``minimize`` happens before the sink is called (nothing is
        recorded for a refused request).
        """
        if not isinstance(project_id, uuid.UUID):
            raise TypeError("project_id must be a uuid.UUID")
        if not isinstance(provider_kinds, frozenset):
            raise TypeError("provider_kinds must be a frozenset")
        if not provider_kinds:
            raise ValueError("provider_kinds must not be empty")
        if not all(isinstance(kind, ProviderKind) for kind in provider_kinds):
            raise TypeError("provider_kinds must contain only ProviderKind values")
        minimized = self.minimize(draft, context)
        moment = self._clock()
        record = ExternalSendRecord(
            recorded_at=(
                moment.astimezone(UTC)
                if isinstance(moment, datetime) and moment.utcoffset() is not None
                else moment
            ),
            project_id=project_id,
            query_fingerprint=minimized.fingerprint,
            query_chars=len(minimized.query),
            provider_kinds=tuple(sorted(provider_kinds, key=KIND_ORDER.__getitem__)),
            withheld=minimized.withheld,
            credentials_removed=minimized.credentials_removed,
            pieces_matched=minimized.pieces_matched,
            abstractions=minimized.abstractions,
            truncated=minimized.truncated,
        )
        try:
            async with asyncio.timeout(self._audit_timeout):
                await self._audit.record(record)
        except Exception as error:  # the sink is foreign code: fail closed
            logger.warning(
                "external send audit failed: exception_type=%s", type(error).__name__
            )
            raise PrivacyRefusal(RefusalReason.AUDIT_FAILED) from None
        return minimized

    async def preflight(
        self,
        request: ResearchRequest,
        kinds: frozenset[ProviderKind],
        subject: object,
    ) -> ResearchRequest:
        """The ``SearchPreflight`` hook of ``ResearchBroker.gather``.

        ``subject`` must be a ``PrivacyInput`` (``None`` or anything else is a
        ``PrivacyRefusal(UNCLASSIFIED_CONTEXT)``: default deny). The draft is
        ``request.query``; ``kinds`` are the provider kinds that will be queried.
        Returns ``request`` with its query replaced by the minimised, recorded
        query; every other field is unchanged.
        """
        if not isinstance(request, ResearchRequest):
            raise TypeError("request must be a ResearchRequest")
        if not isinstance(subject, PrivacyInput):
            raise PrivacyRefusal(RefusalReason.UNCLASSIFIED_CONTEXT)
        minimized = await self.authorize(
            request.query,
            subject.context,
            project_id=subject.project_id,
            provider_kinds=kinds,
        )
        return replace(request, query=minimized.query)
