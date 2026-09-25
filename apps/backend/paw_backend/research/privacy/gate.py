"""``PrivacyGate``: what may be sent to an external search, and the audit of it.

The gate decides; the small pure functions in ``rules.py`` do the text work. This
module keeps everything that must not depend on those functions being right: the
validation of the caller's input, the default-deny handling of unclassified
context, the fixed order of the steps, the independent safety checks on the
finished query, and "audit first, then send".

Order of ``PrivacyGate.minimize`` (the draft is the caller's proposed query):

1. Refuse an unclassified context, a draft over ``MAX_DRAFT_CHARS`` and a context
   over the limits, before any text work. The limits are checked twice: on the
   text as written (cheap) and on its size after NFKC and full case folding
   (``folded_length``), because those can make a text much longer (U+FDFA is one
   code point and 18 after NFKC) and every later step works on that form.
2. ``normalize_text`` the draft.
3. ``strip_credentials``, FIRST: a credential is removed whole from the draft as
   written. Nothing that cuts text (step 4) may run before it, because a credential
   cut into pieces (``ghp_ABCDEF`` and ``abcdefghij``) is no longer recognised by
   this rule or by the final check, and its pieces would be sent.
4. For every non-public piece (in the order given): ``normalize_text`` its text
   and find the spans of the draft copied from it with ``find_copied_spans``
   (window ``copy_window(label)``, shortened to the length of the piece, both
   counted in case-folded characters: matching uses full Unicode case folding,
   so ``ß`` and ``SS`` are the same). A span that touches a word that one of the
   abstraction rules of step 6 would rewrite (a path, an address, an identifier,
   a URL, ...) is widened to the whole word: the same hazard as in step 3, for
   the words those rules remove (``an-2026.md`` left of a private path). All spans
   are merged and replaced by spaces in one go.
5. ``strip_credentials`` again, when step 4 changed the text: removing a copy can
   uncover a credential that a neighbouring character had hidden from the rule.
6. The abstraction rules, in this order: ``abstract_urls``, ``abstract_emails``,
   ``abstract_paths``, ``abstract_hosts``, ``abstract_ids``,
   ``drop_opaque_tokens``, ``generalize_versions``.
   After step 5 and after every one of these rules, ``redact_text`` must find no
   MORE credentials than before the step (a rule that produced one is refused at
   once, before a later rule can cut it into pieces that the final check would
   not recognise).
7. ``truncate_query`` to ``MAX_MINIMIZED_QUERY_CHARS``.
8. Safety checks that do not use ``rules.py``: the query has a word character;
   ``redact_text`` finds no credential in it; no whole non-public piece and no
   word of 4 or more characters of a secret is in it (compared after NFKC,
   removal of format characters and full case folding with ``str.casefold``,
   the same folding as the copy detection but not taken from ``rules.py``).

Nothing in an exception, a log line or a record contains the draft, a piece, the
query, an exception text of the audit sink or a name that the sink chose (the log
line names the class of the sink's exception only through the provider broker's
closed ``log_type_name``).
"""

import asyncio
import inspect
import logging
import re
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
    MAX_PIECE_CHARS,
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
from paw_backend.research.providers.broker import log_type_name
from paw_backend.research.providers.contract import (
    KIND_ORDER,
    ProviderKind,
    ResearchRequest,
)
from paw_backend.tools.credentials import redact_text

logger = logging.getLogger(__name__)

_WORD = re.compile(r"\S+")

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


def widen_to_whole_words(
    text: str,
    spans: Sequence[tuple[int, int]],
    rewritten: Callable[[str], bool],
) -> tuple[tuple[int, int], ...]:
    """``spans`` (sorted, disjoint, inside ``text``) with every span that touches a
    word for which ``rewritten(word)`` is true widened to that whole word.

    A word is a maximal run of non-space characters. A span that covers a word
    entirely, or touches only words for which ``rewritten`` is false, is unchanged.
    The result is merged (sorted, no two spans touch or overlap). The number of
    calls to ``rewritten`` is at most the number of words, so the time is linear in
    ``len(text)`` plus the cost of ``rewritten``."""
    widened = list(spans)
    index = 0
    for match in _WORD.finditer(text):
        start, end = match.span()
        while index < len(spans) and spans[index][1] <= start:
            index += 1
        if index == len(spans):
            break
        if spans[index][0] >= end:
            continue  # no span touches this word
        if spans[index][0] <= start and end <= spans[index][1]:
            continue  # a span covers it whole already
        if rewritten(match.group()):
            widened.append((start, end))
    return merge_spans(widened)


def _rewritten_by_the_rules(word: str) -> bool:
    """Whether one of the abstraction rules rewrites or removes (part of) ``word``.

    The rules work on whitespace-separated words, so a word is judged alone."""
    return any(getattr(rules, name)(word)[1] for name in ABSTRACTION_RULE_NAMES)


def folded_length(text: str) -> int:
    """The size of ``text`` in the form the filter compares: NFKC, then full case
    folding, counted in characters.

    This is what the limits of the gate count (Decision 0010): NFKC can turn one
    code point into up to 18 (U+FDFA) and case folding into up to 3, and the copy
    detection, the safety checks and the windows all work on that form. It never
    depends on ``rules.py`` (a gate check must not depend on the functions it
    guards) and it is an upper bound of the length of ``rules.normalize_text``
    plus ``rules.fold_for_match``, which only drop format characters and collapse
    spaces. The text is normalised once and dropped: the memory is a few times the
    length of ``text`` as written (at most 18 times), never more."""
    return len(unicodedata.normalize("NFKC", text).casefold())


def _context_within_limits(context: Sequence[ContextPiece]) -> bool:
    """Whether the pieces are within ``MAX_PIECE_CHARS`` each and
    ``MAX_TOTAL_CONTEXT_CHARS`` in all, counted with ``folded_length``.

    The pieces are measured one at a time and the loop stops at the first one that
    breaks a limit, so an expanding context is never normalised as a whole. The
    size is that of every piece of every label: public text is never read by the
    filter, but it counts too (one rule, nothing to forget if that changes)."""
    total = 0
    for piece in context:
        size = folded_length(piece.text)
        total += size
        if size > MAX_PIECE_CHARS or total > MAX_TOTAL_CONTEXT_CHARS:
            return False
    return True


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
        * a draft of more than ``MAX_DRAFT_CHARS`` characters, as written OR
          after NFKC and full case folding (``folded_length``): ``DRAFT_TOO_LONG``;
        * more than ``MAX_CONTEXT_PIECES`` pieces, or more than
          ``MAX_TOTAL_CONTEXT_CHARS`` characters of text in all, or a piece of
          more than ``MAX_PIECE_CHARS`` characters, counted as written and after
          NFKC and full case folding (``folded_length``): ``CONTEXT_TOO_LARGE``.
          A text that expands (U+FDFA becomes 18 characters) is refused before any
          of it is normalised by ``rules.py`` or compared;
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
        # The raw length is the cheap early reject; the folded length is the limit
        # that protects the work below (an expanding text is as long as it
        # becomes, not as it is written).
        if len(draft) > MAX_DRAFT_CHARS or folded_length(draft) > MAX_DRAFT_CHARS:
            raise PrivacyRefusal(RefusalReason.DRAFT_TOO_LONG)
        if (
            len(context) > MAX_CONTEXT_PIECES
            or sum(len(piece.text) for piece in context) > MAX_TOTAL_CONTEXT_CHARS
            or not _context_within_limits(context)
        ):
            raise PrivacyRefusal(RefusalReason.CONTEXT_TOO_LARGE)

        text = rules.normalize_text(draft)
        # The credential rule runs first (module docstring, step 3): nothing that
        # cuts text may run before it.
        text, credentials_removed = rules.strip_credentials(text)
        # ``credentials`` is what ``redact_text`` finds in the text now; a step may
        # not increase it (``_no_new_credential``).
        credentials = redact_text(text)[1]
        text, pieces_matched = self._remove_copied_text(text, context)
        if pieces_matched:  # a copy was removed: the text changed
            text, uncovered = rules.strip_credentials(text)
            credentials_removed += uncovered
            credentials = self._no_new_credential(text, credentials, changed=True)
        abstractions = 0
        for name in ABSTRACTION_RULE_NAMES:
            before = text
            text, count = getattr(rules, name)(text)
            abstractions += count
            credentials = self._no_new_credential(
                text, credentials, changed=text != before
            )
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
        merged = widen_to_whole_words(text, merge_spans(spans), _rewritten_by_the_rules)
        return rules.normalize_text(replace_spans(text, merged)), matched

    @staticmethod
    def _no_new_credential(text: str, before: int, *, changed: bool) -> int:
        """The number of credentials in ``text``; a refusal if it is above ``before``.

        Called after every step that can change the text. A step that leaves a
        credential where there was none (or more than there were) is a faulty
        step: refuse now, because a later step that cut the credential into pieces
        would hide it from ``_check_finished_query``. Only a changed text is
        looked at again."""
        if not changed:
            return before
        found = redact_text(text)[1]
        if found > before:
            raise PrivacyRefusal(RefusalReason.CREDENTIAL_REMAINS)
        return found

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
        that was not recorded. The failure is logged once at WARNING with a fixed
        exception type only: ``log_type_name`` of the provider broker, i.e. the name
        of a builtin or ``paw_backend.research`` exception class (``TimeoutError``
        for a sink that is too slow) and ``adapter_error`` for every other class
        (a sink's own, a subclass, one that is only named like a builtin). The
        class of the sink's exception is never asked for its name or any other
        attribute, so a sink cannot put a credential or a newline into the log, and
        a metaclass hook that raises cannot replace the refusal.
        ``asyncio.CancelledError`` is never swallowed.
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
            # The class of ``error`` is the sink's data (its name, its metaclass):
            # log a fixed value for it, never a name it chose, and never read a
            # hook of it, so nothing here can raise instead of the refusal.
            logger.warning(
                "external send audit failed: exception_type=%s", log_type_name(error)
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
