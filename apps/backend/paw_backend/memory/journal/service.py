"""The Immediate Journal: save the raw message and the Pending Observation at once.

REQUIREMENTS.md "Immediate Journal / Background Consolidation": memory handling is
split into a synchronous save and an asynchronous consolidation. This is the
synchronous half. It needs no GPU and no model: every method is PostgreSQL only.

``record_user_message`` saves, in ONE transaction,

1. the Raw Conversation message (``messages``, role ``user``),
2. the journal entry with its ``event_sequence``, ``turn_id``, the owner and the
   project / repo context, in state ``pending`` (the Pending Observation), and
3. the consolidation job (``memory_consolidation_queue``) with the priority.

All three commit or none does, so a message without its observation (or the other
way round) cannot exist, and a worker that is not running does not matter: the
observation is in the database when the assistant starts to answer.

Event sequence. The number of an event is assigned under a row lock: the
conversation row is locked ``FOR NO KEY UPDATE`` first, and the number is one
above the highest ``messages.event_sequence`` of the conversation (0 for the
first). Writers of one conversation therefore take the lock one after another, so
the numbers are unique, gapless and in commit order however many run at once, and
a conversation that is being deleted cannot receive a message. The unique
constraint ``(conversation_id, event_sequence)`` of ``messages`` is the last
defence: every message of a conversation must be appended through this class (or
the same lock), because a writer that picks its own number would either collide or
break the order. ``append_message`` is the path for the assistant, tool, agent and
task events, so that the whole conversation shares one sequence.

The next turn. ``pending_observations`` returns the observations of a conversation
that are not consolidated yet, in event order: the context builder reads them
together with the session state and the active memories, so that an instruction is
not lost while the worker is behind (or the GPU is off).

Authorization. Every method needs ``memory.use`` (``Scope.SELF``) on the actor's own
data; the actor must own the conversation, and another user's conversation is "not
found". Arguments are validated first, then the actor is authorized, then the
database is used. ``record_user_message`` takes a human ``Principal`` only: an agent
must not write a user's words. The methods that do not put words in a user's mouth
also take an ``AgentActor`` (the delegating user's grant must list ``memory.use``).

Privacy. Errors, logs and the audit row (the Authorizer's, written with ids and
the capability only) never contain message text. ``PendingObservation`` keeps its
text out of ``repr``.
"""

from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text

from paw_backend.authz import (
    Authorizer,
    Capability,
    Decision,
    Principal,
    Resource,
)
from paw_backend.db import Database
from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import (
    AppendedMessage,
    EntryState,
    JournalReceipt,
    PendingObservation,
    Priority,
    SyncStatus,
)
from paw_backend.memory.journal.errors import (
    ConversationNotFoundError,
    InputProblem,
    InvalidJournalInputError,
    JournalPermissionError,
)
from paw_backend.memory.journal.models import JournalEntry
from paw_backend.memory.journal.queue import insert_job
from paw_backend.memory.journal.sql import inlined, transaction
from paw_backend.memory.journal.validation import (
    validate_enum,
    validate_int,
    validate_optional_uuid,
    validate_text,
    validate_uuid,
)
from paw_backend.memory.models import Conversation, Message, MessageRole
from paw_backend.memory.shared.records import AgentActor

RESOURCE_KIND = "memory_journal"

_ENTRY = JournalEntry.__table__
_MESSAGE = Message.__table__
_CONVERSATION = Conversation.__table__

Actor = Principal | AgentActor

# The four labels of the UI ("同期済み / N件を整理中 / GPU待ち / 処理失敗・再試行",
# REQUIREMENTS.md), over the pending entries of one conversation and the latest
# job of each.
_SYNC_STATUS = text(
    """
    SELECT
        count(*) FILTER (
            WHERE j.status = 'claimed'
               OR (j.status = 'queued' AND j.last_failure IS NULL)
        ) AS consolidating,
        count(*) FILTER (
            WHERE j.status = 'queued' AND j.last_failure = 'worker_unavailable'
        ) AS waiting_for_worker,
        count(*) FILTER (
            WHERE j.status = 'queued' AND j.last_failure IS NOT NULL
              AND j.last_failure <> 'worker_unavailable'
        ) AS retrying,
        count(*) FILTER (
            WHERE j.id IS NULL OR j.status NOT IN ('queued', 'claimed')
        ) AS failed
    FROM memory_journal_entries e
    LEFT JOIN LATERAL (
        SELECT q.id, q.status, q.last_failure
        FROM memory_consolidation_queue q
        WHERE q.entry_id = e.id
        ORDER BY q.id DESC
        LIMIT 1
    ) j ON true
    WHERE e.conversation_id = :conversation_id
      AND e.owner_user_id = :owner_user_id
      AND e.state = 'pending'
    """
)


class MemoryJournal:
    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        *,
        lock_timeout_ms: int = limits.DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """Validate the collaborators up front; a wrong one fails here, loudly.

        ``authorizer`` needs callable ``authorize`` and ``authorize_agent_action``.
        ``lock_timeout_ms``: 1 to ``MAX_LOCK_TIMEOUT_MS``.
        """
        if not isinstance(database, Database):
            raise InvalidJournalInputError("database", InputProblem.WRONG_TYPE)
        for name in ("authorize", "authorize_agent_action"):
            if not callable(getattr(authorizer, name, None)):
                raise InvalidJournalInputError("authorizer", InputProblem.WRONG_TYPE)
        self._database = database
        self._authorizer = authorizer
        self._lock_timeout_ms = validate_int(
            "lock_timeout_ms", lock_timeout_ms, low=1, high=limits.MAX_LOCK_TIMEOUT_MS
        )

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _check_actor(actor: object, *, human_only: bool = False) -> None:
        allowed = Principal if human_only else Actor
        if not isinstance(actor, allowed):
            raise InvalidJournalInputError("actor", InputProblem.WRONG_TYPE)

    async def _authorize(self, actor: Actor, conversation_id: UUID) -> UUID:
        """Authorize ``memory.use`` on the actor's own journal; return the user's id."""
        if isinstance(actor, AgentActor):
            user_id = actor.delegator_id
            decision = await self._authorizer.authorize_agent_action(
                user_id,
                actor.grant,
                Capability.MEMORY_USE,
                Resource.owned_by(user_id, RESOURCE_KIND, conversation_id),
            )
        else:
            user_id = actor.user_id
            decision = await self._authorizer.authorize(
                actor,
                Capability.MEMORY_USE,
                Resource.owned_by(user_id, RESOURCE_KIND, conversation_id),
            )
        if not isinstance(decision, Decision):
            raise JournalPermissionError("invalid_decision")
        if not decision.allowed:
            raise JournalPermissionError(decision.reason.value)
        return user_id

    def _transaction(self):
        return transaction(self._database, self._lock_timeout_ms)

    @staticmethod
    async def _lock_conversation(session, conversation_id: UUID, user_id: UUID):
        """Lock the user's conversation; ``ConversationNotFoundError`` if not theirs."""
        row = (
            await session.execute(
                select(
                    _CONVERSATION.c.project_id,
                    _CONVERSATION.c.repo_id,
                )
                .where(
                    _CONVERSATION.c.id == conversation_id,
                    _CONVERSATION.c.owner_user_id == user_id,
                )
                # FOR NO KEY UPDATE: writers of one conversation queue up here, but
                # the foreign-key checks of the messages (FOR KEY SHARE) do not.
                .with_for_update(key_share=True)
            )
        ).first()
        if row is None:
            raise ConversationNotFoundError
        return row

    @staticmethod
    async def _append(
        session, conversation_id: UUID, turn_id: UUID, role: MessageRole, content: str
    ) -> tuple[UUID, int]:
        """Add a raw message with the next event sequence (conversation locked)."""
        sequence = (
            await session.execute(
                select(
                    func.coalesce(func.max(_MESSAGE.c.event_sequence), -1) + 1
                ).where(_MESSAGE.c.conversation_id == conversation_id)
            )
        ).scalar_one()
        message_id = (
            await session.execute(
                insert(_MESSAGE)
                .values(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    event_sequence=sequence,
                    role=role.value,
                    content=content,
                )
                .returning(_MESSAGE.c.id)
            )
        ).scalar_one()
        return message_id, sequence

    # -- writing -----------------------------------------------------------------

    async def record_user_message(
        self,
        actor: Principal,
        conversation_id: UUID,
        content: str,
        *,
        turn_id: UUID | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> JournalReceipt:
        """Save a user message and its Pending Observation, and queue its consolidation.

        One transaction: the Raw message, the journal entry (state ``pending``) and
        the job. ``turn_id`` names the turn (a new one if ``None``); the assistant's
        reply of the turn is appended with the same ``turn_id``. ``priority`` is the
        queue class the CALLER decides: ``HIGH`` for an explicit preference or
        decision the next turn needs, ``NORMAL`` otherwise (Decision 0018); the
        journal does not read the text to decide.

        Raises :class:`InvalidJournalInputError` (before anything else),
        :class:`JournalPermissionError`, :class:`ConversationNotFoundError` (no such
        conversation of this user) and :class:`JournalBusyError` (a lock timed out;
        nothing was saved, retry).
        """
        self._check_actor(actor, human_only=True)
        conversation_id = validate_uuid("conversation_id", conversation_id)
        content = validate_text(
            "content", content, max_chars=limits.MAX_USER_MESSAGE_CHARS
        )
        turn = validate_optional_uuid("turn_id", turn_id)
        priority = validate_enum("priority", priority, Priority)
        user_id = await self._authorize(actor, conversation_id)
        if turn is None:
            turn = uuid4()
        async with self._transaction() as session:
            context = await self._lock_conversation(session, conversation_id, user_id)
            message_id, sequence = await self._append(
                session, conversation_id, turn, MessageRole.USER, content
            )
            entry_id = (
                await session.execute(
                    insert(_ENTRY)
                    .values(
                        conversation_id=conversation_id,
                        message_id=message_id,
                        turn_id=turn,
                        event_sequence=sequence,
                        owner_user_id=user_id,
                        project_id=context.project_id,
                        repo_id=context.repo_id,
                    )
                    .returning(_ENTRY.c.id)
                )
            ).scalar_one()
            await insert_job(session, entry_id, priority)
        return JournalReceipt(
            entry_id=entry_id,
            message_id=message_id,
            conversation_id=conversation_id,
            turn_id=turn,
            event_sequence=sequence,
            priority=priority,
        )

    async def append_message(
        self,
        actor: Actor,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
        *,
        turn_id: UUID,
    ) -> AppendedMessage:
        """Save an assistant, tool, agent or task event in the conversation's sequence.

        Raw Conversation only: no observation and no job. ``role`` may not be
        ``user`` (``record_user_message`` is the one way in for a user's words).
        """
        self._check_actor(actor)
        conversation_id = validate_uuid("conversation_id", conversation_id)
        role = validate_enum("role", role, MessageRole)
        if role is MessageRole.USER:
            raise InvalidJournalInputError("role", InputProblem.OUT_OF_RANGE)
        content = validate_text(
            "content", content, max_chars=limits.MAX_OTHER_MESSAGE_CHARS
        )
        turn = validate_uuid("turn_id", turn_id)
        user_id = await self._authorize(actor, conversation_id)
        async with self._transaction() as session:
            await self._lock_conversation(session, conversation_id, user_id)
            message_id, sequence = await self._append(
                session, conversation_id, turn, role, content
            )
        return AppendedMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            turn_id=turn,
            event_sequence=sequence,
        )

    # -- reading -----------------------------------------------------------------

    async def _check_conversation(self, session, conversation_id: UUID, user_id: UUID):
        found = (
            await session.execute(
                select(_CONVERSATION.c.id).where(
                    _CONVERSATION.c.id == conversation_id,
                    _CONVERSATION.c.owner_user_id == user_id,
                )
            )
        ).first()
        if found is None:
            raise ConversationNotFoundError

    async def pending_observations(
        self,
        actor: Actor,
        conversation_id: UUID,
        *,
        limit: int = limits.DEFAULT_PENDING_LIMIT,
    ) -> list[PendingObservation]:
        """The conversation's observations not consolidated yet, oldest first.

        What the next turn reads next to the session state and the active memories,
        so that an instruction is not lost while consolidation is behind. Includes
        entries whose job is dead: they are still pending. At most ``limit`` (1 to
        ``MAX_PENDING_LIMIT``) are returned, by ``event_sequence``.
        """
        self._check_actor(actor)
        conversation_id = validate_uuid("conversation_id", conversation_id)
        limit = validate_int("limit", limit, low=1, high=limits.MAX_PENDING_LIMIT)
        user_id = await self._authorize(actor, conversation_id)
        query = (
            select(
                _ENTRY.c.id,
                _ENTRY.c.turn_id,
                _ENTRY.c.event_sequence,
                _ENTRY.c.recorded_at,
                _MESSAGE.c.content,
            )
            .join(_MESSAGE, _MESSAGE.c.id == _ENTRY.c.message_id)
            .where(
                _ENTRY.c.conversation_id == conversation_id,
                _ENTRY.c.owner_user_id == user_id,
                # Written into the statement so that the partial index serves it.
                _ENTRY.c.state == inlined("state_pending", EntryState.PENDING.value),
            )
            .order_by(_ENTRY.c.event_sequence)
            .limit(limit)
        )
        async with self._database.session() as session:
            await self._check_conversation(session, conversation_id, user_id)
            rows = (await session.execute(query)).all()
        return [
            PendingObservation(
                entry_id=row.id,
                conversation_id=conversation_id,
                turn_id=row.turn_id,
                event_sequence=row.event_sequence,
                recorded_at=row.recorded_at,
                content=row.content,
            )
            for row in rows
        ]

    async def sync_status(self, actor: Actor, conversation_id: UUID) -> SyncStatus:
        """The memory sync state of the conversation: what the UI shows.

        Counts the pending observations by what happens to them: being worked on,
        waiting for the worker (the GPU was not reachable), retrying after a
        failure, failed (dead letter). All zero means synced.
        """
        self._check_actor(actor)
        conversation_id = validate_uuid("conversation_id", conversation_id)
        user_id = await self._authorize(actor, conversation_id)
        async with self._database.session() as session:
            await self._check_conversation(session, conversation_id, user_id)
            row = (
                await session.execute(
                    _SYNC_STATUS,
                    {"conversation_id": conversation_id, "owner_user_id": user_id},
                )
            ).one()
        return SyncStatus(
            consolidating=row.consolidating,
            waiting_for_worker=row.waiting_for_worker,
            retrying=row.retrying,
            failed=row.failed,
        )
