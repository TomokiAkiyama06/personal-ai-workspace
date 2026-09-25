"""``ConnectionService.execute``: the call, its attribution, settlement, refusals."""

import asyncio
import logging
import unittest
import uuid

from sqlalchemy import text

from paw_backend.authz import Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    AdapterFailure,
    AdapterResult,
    ConnectionCallError,
    ConnectionKind,
    ConnectionPermissionDeniedError,
    ConnectionUnavailableError,
    FailureCode,
    QuotaExceededError,
    QuotaNotConfiguredError,
    RefusalReason,
    TaskNotUsableError,
    UsagePurpose,
)
from paw_backend.connections.store import Admitted
from paw_backend.tasks import TaskRun

from .connections_fakes import CANARY, handle
from .connections_support import (
    FailingSink,
    PostgresConnectionTestCase,
    requires_postgres,
)

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE


class ExecuteCase(PostgresConnectionTestCase):
    """A connected Codex connection, a quota of 5 requests a day and one task."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        self.seed_quota(self.user, 5)
        self.task = self.seed_task(self.user)
        self.project = self.project_of(self.task)

    def call(self, **overrides):
        """The coroutine of one call (the arguments can be replaced by keyword)."""
        arguments = {
            "principal": self.principal(self.user),
            "context": self.context(self.task, self.user, self.project),
            "kind": CODEX,
            "request": self.request(),
        }
        arguments.update(overrides)
        return self.service.execute(**arguments)

    def refusals(self):
        return [e for e in self.sink.events if e.action == "connection.use"]


@requires_postgres
class HappyPathTest(ExecuteCase):
    async def test_the_answer_is_returned_and_the_call_is_attributed(self):
        result = await self.call()

        self.assertEqual(result.text, "an answer")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 5))
        self.assertEqual(result.redactions, 0)
        (row,) = self.usage_rows()
        self.assertEqual(row["id"], result.usage_id)
        self.assertEqual(row["user_id"], self.user)
        self.assertEqual(row["task_id"], self.task)
        self.assertEqual(row["project_id"], self.project)
        self.assertEqual(
            (row["kind"], row["model"], row["purpose"], row["status"]),
            ("codex", "test-model-1", "coding", "succeeded"),
        )
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (10, 5))
        self.assertIsNone(row["failure_code"])
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual(result.duration_ms, row["duration_ms"])
        self.assertGreaterEqual(row["finished_at"], row["started_at"])

    async def test_the_adapter_gets_the_request_and_the_credential_only(self):
        await self.call(
            request=self.request(model="m-2", prompt="Do it.", timeout_seconds=7)
        )
        (request,) = self.codex.requests
        self.assertEqual(
            (request.model, request.prompt, request.timeout_seconds),
            ("m-2", "Do it.", 7.0),
        )
        self.assertEqual(self.codex.revealed, [CANARY])
        self.assertEqual(self.resolver.resolved, [handle(1)])
        self.assertEqual(self.claude.requests, [])

    async def test_every_purpose_is_recorded_as_its_category(self):
        for purpose in UsagePurpose:
            with self.subTest(purpose=purpose.value):
                await self.call(request=self.request(purpose=purpose))
        self.assertEqual(
            [row["purpose"] for row in self.usage_rows()],
            [p.value for p in UsagePurpose],
        )

    async def test_the_call_is_started_and_ended_by_the_database_clock(self):
        before = self.scalar("SELECT clock_timestamp()")
        await self.call()
        after = self.scalar("SELECT clock_timestamp()")
        (row,) = self.usage_rows()
        self.assertLessEqual(before, row["started_at"])
        self.assertLessEqual(row["started_at"], row["finished_at"])
        self.assertLessEqual(row["finished_at"], after)

    async def test_the_row_exists_in_flight_while_the_call_runs(self):
        gate = asyncio.Event()
        self.codex.gate = gate
        running = self.spawn(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)

        (row,) = self.usage_rows()
        self.assertEqual(row["status"], "in_flight")
        self.assertEqual(
            (
                row["finished_at"],
                row["duration_ms"],
                row["input_tokens"],
                row["output_tokens"],
            ),
            (None, None, None, None),
        )
        gate.set()
        result = await running
        self.assertEqual(self.usage_rows()[0]["status"], "succeeded")
        self.assertEqual(self.usage_rows()[0]["id"], result.usage_id)

    async def test_unknown_token_counts_stay_unknown(self):
        self.codex.input_tokens = self.codex.output_tokens = None
        result = await self.call()
        self.assertEqual((result.input_tokens, result.output_tokens), (None, None))
        (row,) = self.usage_rows()
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (None, None))

    async def test_an_empty_answer_and_zero_tokens_are_a_valid_result(self):
        self.codex.text, self.codex.input_tokens, self.codex.output_tokens = "", 0, 0
        result = await self.call()
        self.assertEqual(
            (result.text, result.input_tokens, result.output_tokens), ("", 0, 0)
        )

    async def test_each_call_of_a_task_is_its_own_record(self):
        first, second = await self.call(), await self.call()
        self.assertNotEqual(first.usage_id, second.usage_id)
        self.assertEqual({r["task_id"] for r in self.usage_rows()}, {self.task})
        self.assertEqual(len(self.usage_rows()), 2)

    async def test_every_live_state_of_a_task_may_use_the_connection(self):
        for state in ("queued", "running", "waiting", "paused", "evaluating"):
            with self.subTest(state=state):
                task = self.seed_task(self.user, state=state)
                await self.call(
                    context=self.context(task, self.user, self.project_of(task))
                )
        self.assertEqual(len(self.usage_rows()), 5)

    async def test_the_other_kind_uses_its_own_connection_adapter_and_quota(self):
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        self.seed_quota(self.user, 2, kind=CLAUDE)
        result = await self.call(kind=CLAUDE)
        self.assertEqual(self.claude.revealed, [CANARY + "-second"])
        self.assertEqual(self.codex.revealed, [])
        self.assertEqual(self.usage_rows()[0]["kind"], "claude")
        self.assertEqual(result.text, "an answer")

    async def test_an_allowed_call_is_audited_by_the_authorizer_and_by_its_usage_row(
        self,
    ):
        await self.call()
        self.assertEqual(
            self.audit_actions(), [("agent.use", "allow", "granted_to_resource_owner")]
        )
        (event,) = self.sink.events
        self.assertEqual((event.actor_id, event.actor_role), (self.user, "user"))
        self.assertEqual(event.resource_kind, "connection")

    async def test_an_owner_uses_the_connection_for_their_own_task_like_anyone(self):
        task = self.seed_task(self.owner)
        self.seed_quota(self.owner, None)
        await self.call(
            principal=self.principal(self.owner),
            context=self.context(task, self.owner, self.project_of(task)),
        )
        self.assertEqual(self.usage_rows(self.owner)[0]["user_id"], self.owner)

    async def test_the_result_does_not_print_the_answer(self):
        self.codex.text = "the private answer"
        result = await self.call()
        self.assertNotIn("private", repr(result))
        self.assertEqual(result.text, "the private answer")


@requires_postgres
class SecretIsolationTest(ExecuteCase):
    """The credential reaches the adapter and nobody else (acceptance criterion)."""

    async def run_everything_and_collect(self, **adapter_changes):
        for name, value in adapter_changes.items():
            setattr(self.codex, name, value)
        outputs = []
        with self.assertLogs("paw_backend", logging.DEBUG) as logs:
            logging.getLogger("paw_backend").debug("start")
            try:
                result = await self.call()
                outputs += [repr(result), result.text]
            except Exception as error:
                outputs += [repr(error), str(error), repr(error.args)]
                self.error = error
        outputs.append("\n".join(logs.output))
        outputs.append(self.everything_stored())
        outputs += [repr(event) for event in self.sink.events]
        outputs.append(repr(await self.service.availability(self.principal(self.user))))
        outputs.append(
            repr(await self.service.get_connection(self.principal(self.admin), CODEX))
        )
        outputs.append(
            repr(await self.service.list_usage(self.principal(self.user), self.user))
        )
        return "\n".join(outputs)

    async def test_the_plaintext_is_nowhere_but_in_the_adapter(self):
        everything = await self.run_everything_and_collect()
        self.assertEqual(self.codex.revealed, [CANARY])
        self.assertNotIn(CANARY, everything)
        self.assertNotIn(CANARY[:16], everything)

    async def test_the_handle_is_not_shown_to_a_user_or_an_agent(self):
        everything = await self.run_everything_and_collect()
        self.assertNotIn(handle(1), everything.replace(self.everything_stored(), ""))

    async def test_an_answer_that_repeats_the_credential_is_scrubbed(self):
        self.codex.text = f"My key is {CANARY}, do not share {CANARY}."
        result = await self.call()
        self.assertNotIn(CANARY, result.text)
        self.assertEqual(result.text, "My key is [REDACTED], do not share [REDACTED].")
        self.assertEqual(result.redactions, 0)  # the exact-value scrub is not a pattern

    async def test_a_recognisable_credential_format_in_an_answer_is_redacted(self):
        token = "ghp_" + "A1b2C3d4E5" * 4
        self.codex.text = f"token={token} done"
        result = await self.call()
        self.assertNotIn(token, result.text)
        self.assertIn("[REDACTED]", result.text)
        self.assertGreaterEqual(result.redactions, 1)

    async def test_an_exception_that_holds_the_credential_is_not_repeated(self):
        everything = await self.run_everything_and_collect(
            error=RuntimeError("upstream said: " + CANARY)
        )
        self.assertIsInstance(self.error, ConnectionCallError)
        self.assertNotIn(CANARY, everything)
        self.assertNotIn("upstream said", everything)
        self.assertIsNone(self.error.__cause__)
        self.assertIsNone(self.error.__context__)

    async def test_a_resolver_that_fails_with_the_credential_in_its_message(self):
        self.resolver.error = KeyError("no such handle " + CANARY)
        everything = await self.run_everything_and_collect()
        self.assertNotIn(CANARY, everything)
        self.assertNotIn("no such handle", everything)
        self.assertEqual(self.codex.revealed, [])

    async def test_the_adapter_is_handed_a_secret_object_never_the_handle_text(self):
        seen = []

        async def spy(secret, request):
            seen.append((type(secret).__name__, repr(secret), repr(request)))
            return AdapterResult("ok")

        self.codex.run = spy
        await self.call()
        ((kind, secret_repr, request_repr),) = seen
        self.assertEqual(kind, "Secret")
        self.assertEqual(secret_repr, "Secret(<redacted>)")
        self.assertNotIn(handle(1), request_repr)
        self.assertNotIn(
            "Fix the parser", request_repr
        )  # the prompt is not in the repr

    async def test_an_admission_does_not_print_the_handle(self):
        admitted = Admitted(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), handle(1))
        self.assertNotIn(handle(1), repr(admitted))
        self.assertNotIn("cred_", str(admitted))

    async def test_no_column_of_any_table_holds_the_prompt_or_the_answer(self):
        self.codex.text = "ANSWER-MARKER-XYZ"
        await self.call(request=self.request(prompt="PROMPT-MARKER-XYZ"))
        stored = self.everything_stored()
        self.assertNotIn("PROMPT-MARKER", stored)
        self.assertNotIn("ANSWER-MARKER", stored)
        self.assertNotIn("PROMPT-MARKER", "\n".join(repr(e) for e in self.sink.events))


@requires_postgres
class FailureTest(ExecuteCase):
    async def failing(self, error: BaseException, **request):
        self.codex.error = error
        with self.assertLogs("paw_backend.connections", logging.WARNING) as logs:
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call(**request)
        return caught.exception, "\n".join(logs.output)

    async def test_a_classified_failure_of_the_adapter_is_recorded_and_raised(self):
        for code in FailureCode:
            with self.subTest(code=code.value):
                self.clear_usage()
                self.clear_connections()  # an expired credential is remembered
                self.seed_connection(CODEX)
                error, _ = await self.failing(AdapterFailure(code))
                self.assertEqual(error.failure, code)
                self.assertEqual(str(error), f"The call failed: {code.value}")
                (row,) = self.usage_rows()
                self.assertEqual(
                    (row["status"], row["failure_code"]), ("failed", code.value)
                )
                self.assertEqual(
                    (row["input_tokens"], row["output_tokens"]), (None, None)
                )
                self.assertIsNotNone(row["finished_at"])
                self.assertGreaterEqual(row["duration_ms"], 0)

    async def test_any_other_exception_is_an_internal_error_and_only_its_type_is_logged(
        self,
    ):
        error, log = await self.failing(RuntimeError("provider said: " + CANARY))
        self.assertEqual(error.failure, FailureCode.INTERNAL_ERROR)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("provider said", log)
        self.assertNotIn(CANARY, log)

    async def test_an_exception_class_of_the_adapter_is_not_named_in_the_log(self):
        class VendorSecretError(Exception):
            pass

        error, log = await self.failing(VendorSecretError("boom"))
        self.assertEqual(error.failure, FailureCode.INTERNAL_ERROR)
        self.assertNotIn("VendorSecretError", log)
        self.assertIn("adapter_error", log)

    async def test_a_timeout_the_adapter_raises_itself_is_a_timeout(self):
        error, _ = await self.failing(TimeoutError("read timed out"))
        self.assertEqual(error.failure, FailureCode.TIMEOUT)

    async def test_a_call_that_outlives_its_deadline_is_a_timeout_and_is_cancelled(
        self,
    ):
        cancelled = []

        async def hang(secret, request):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        self.codex.run = hang
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call(request=self.request(timeout_seconds=0.2))
        self.assertEqual(caught.exception.failure, FailureCode.TIMEOUT)
        self.assertEqual(cancelled, [True])
        self.assertEqual(self.usage_rows()[0]["failure_code"], "timeout")

    async def test_a_resolver_that_fails_is_unavailable_and_the_adapter_never_runs(
        self,
    ):
        self.resolver.error = OSError("vault down")
        with self.assertLogs("paw_backend.connections", logging.WARNING) as logs:
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        self.assertEqual(caught.exception.failure, FailureCode.UNAVAILABLE)
        self.assertEqual(self.codex.requests, [])
        self.assertIn("OSError", "\n".join(logs.output))
        self.assertNotIn("vault down", "\n".join(logs.output))
        self.assertEqual(self.usage_rows()[0]["failure_code"], "unavailable")

    async def test_a_resolver_that_hangs_is_cut_by_the_same_deadline(self):
        async def hang(handle):
            await asyncio.Event().wait()

        self.resolver.resolve = hang
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call(request=self.request(timeout_seconds=0.2))
        self.assertEqual(caught.exception.failure, FailureCode.TIMEOUT)

    async def test_a_resolver_that_returns_anything_but_a_secret_is_unavailable(self):
        for value in (CANARY, b"bytes", 1, {"key": CANARY}):
            with self.subTest(value=type(value).__name__):
                self.clear_usage()
                self.resolver.result = value
                with self.assertLogs("paw_backend.connections", logging.WARNING):
                    with self.assertRaises(ConnectionCallError) as caught:
                        await self.call()
                self.assertEqual(caught.exception.failure, FailureCode.UNAVAILABLE)
                self.assertEqual(self.codex.requests, [])

    async def test_an_adapter_answer_that_is_not_a_result_is_an_invalid_response(self):
        class Lookalike(AdapterResult):
            pass

        forced = AdapterResult("fine")
        object.__setattr__(forced, "input_tokens", -5)
        forced_text = AdapterResult("fine")
        object.__setattr__(forced_text, "text", 123)
        for label, answer in (
            ("a dict", {"text": "x"}),
            ("a str", "x"),
            ("a subclass", Lookalike("x")),
            ("negative tokens forced in", forced),
            ("a non-str text forced in", forced_text),
        ):
            with self.subTest(label):
                self.clear_usage()
                self.codex.result = answer
                with self.assertLogs("paw_backend.connections", logging.WARNING):
                    with self.assertRaises(ConnectionCallError) as caught:
                        await self.call()
                self.assertEqual(caught.exception.failure, FailureCode.INVALID_RESPONSE)
                self.assertEqual(
                    self.usage_rows()[0]["failure_code"], "invalid_response"
                )

    async def test_a_failed_call_still_counts_as_a_request(self):
        self.clear_quotas()
        self.seed_quota(self.user, 1)
        await self.failing(AdapterFailure(FailureCode.RATE_LIMITED))
        # The task has used the connection: it is a running task now (Decision 0016).
        second = self.seed_task(self.user)
        with self.assertRaises(QuotaExceededError):
            await self.call(
                context=self.context(second, self.user, self.project_of(second))
            )

    async def test_an_expired_credential_marks_the_connection_expired(self):
        error, _ = await self.failing(AdapterFailure(FailureCode.EXPIRED))
        self.assertEqual(error.failure, FailureCode.EXPIRED)
        row = self.connection_row(CODEX)
        self.assertEqual(row["status"], "expired")
        self.assertIsNotNone(row["checked_at"])
        self.assertIn(("connection.status", "allow", "expired"), self.audit_actions())
        # ...so the next call is refused before it starts.
        second = self.seed_task(self.user)
        with self.assertRaises(ConnectionUnavailableError):
            await self.call(
                context=self.context(second, self.user, self.project_of(second))
            )

    async def test_a_rate_limit_or_timeout_does_not_mark_the_connection_expired(self):
        for code in (
            FailureCode.RATE_LIMITED,
            FailureCode.UNAVAILABLE,
            FailureCode.TIMEOUT,
        ):
            await self.failing(AdapterFailure(code))
        self.assertEqual(self.connection_row(CODEX)["status"], "connected")

    async def test_an_expiry_of_a_replaced_credential_is_not_applied_to_the_new_one(
        self,
    ):
        gate = asyncio.Event()
        self.codex.gate = gate
        self.codex.error = AdapterFailure(FailureCode.EXPIRED)
        running = self.spawn(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shared_connections SET secret_handle = :h,"
                    " status = 'connected'"
                ),
                {"h": handle(2)},
            )
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            gate.set()
            with self.assertRaises(ConnectionCallError):
                await running
        self.assertEqual(self.connection_row(CODEX)["status"], "connected")

    # -- helpers ---------------------------------------------------------

    def clear_usage(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM connection_usage"))

    def clear_quotas(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM connection_quotas"))


@requires_postgres
class CancellationTest(ExecuteCase):
    async def test_a_cancelled_call_is_recorded_and_the_cancellation_propagates(
        self,
    ):
        cancelled = []

        async def hang(secret, request):
            self.codex.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        self.codex.run = hang
        running = asyncio.ensure_future(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)

        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        self.assertEqual(cancelled, [True])
        (row,) = self.usage_rows()
        self.assertEqual((row["status"], row["failure_code"]), ("cancelled", None))
        self.assertIsNotNone(row["finished_at"])
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (None, None))

    async def test_a_cancelled_call_still_counts_as_a_request(self):
        self.codex.gate = asyncio.Event()
        running = asyncio.ensure_future(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        rows = self.usage_rows()
        self.assertEqual([r["status"] for r in rows], ["cancelled"])

    async def test_a_second_cancellation_during_the_settlement_does_not_lose_it(self):
        self.codex.gate = asyncio.Event()
        running = asyncio.ensure_future(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)
        running.cancel()
        await asyncio.sleep(0)  # the settlement is now in progress
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await self.wait_for(
            lambda: self.usage_rows()[0]["status"] == "cancelled", "the settlement"
        )


@requires_postgres
class SettlementFailureTest(ExecuteCase):
    async def test_a_settlement_that_fails_does_not_lose_the_answer(self):
        async def broken(*args):
            raise RuntimeError("store down " + CANARY)

        self.service._store.settle = broken
        with self.assertLogs("paw_backend.connections", logging.ERROR) as logs:
            result = await self.call()

        self.assertEqual(result.text, "an answer")
        self.assertIsNone(result.duration_ms)
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertIn(str(result.usage_id), text)
        self.assertNotIn(CANARY, text)
        # The row is still there, attributed, and still in flight (no reaper yet).
        (row,) = self.usage_rows()
        self.assertEqual((row["id"], row["status"]), (result.usage_id, "in_flight"))

    async def test_a_failure_to_charge_the_task_budget_does_not_fail_the_call(self):
        # (The budget itself is tested in test_connections_budget.)
        self.assertIsNone(self.service._budget)


@requires_postgres
class RefusalTest(ExecuteCase):
    """Every refusal happens before the call: nothing is written, nothing is run."""

    def assertNothingRan(self):
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])
        self.assertEqual(self.resolver.resolved, [])

    def assertRefusalAudited(self, reason: str, *, project: bool):
        (event,) = self.refusals()
        self.assertEqual((event.decision, event.reason), ("deny", reason))
        self.assertEqual((event.actor_id, event.actor_role), (self.user, "user"))
        self.assertEqual((event.resource_kind, event.resource_id), ("connection", None))
        self.assertEqual(event.project_id, self.project if project else None)

    async def test_a_task_that_does_not_exist(self):
        ghost = uuid.uuid4()
        with self.assertRaises(TaskNotUsableError) as caught:
            await self.call(context=self.context(ghost, self.user, self.project))
        self.assertEqual(caught.exception.reason, RefusalReason.TASK_NOT_FOUND)
        self.assertNothingRan()
        self.assertRefusalAudited("task_not_found", project=False)

    async def test_somebody_elses_task_is_indistinguishable_from_a_missing_one(self):
        stranger = self.seed_user()
        theirs = self.seed_task(stranger)
        with self.assertRaises(TaskNotUsableError) as other:
            await self.call(
                context=self.context(theirs, self.user, self.project_of(theirs))
            )
        with self.assertRaises(TaskNotUsableError) as missing:
            await self.call(context=self.context(uuid.uuid4(), self.user, self.project))
        self.assertEqual(str(other.exception), str(missing.exception))
        self.assertEqual(other.exception.reason, missing.exception.reason)
        self.assertNothingRan()
        self.assertEqual(
            [(e.reason, e.project_id) for e in self.refusals()],
            [("task_not_found", None), ("task_not_found", None)],
        )

    async def test_an_ended_task(self):
        for state in ("completed", "failed", "cancelled"):
            with self.subTest(state=state):
                self.sink.events.clear()
                task = self.seed_task(self.user, state=state)
                project = self.project_of(task)
                with self.assertRaises(TaskNotUsableError) as caught:
                    await self.call(context=self.context(task, self.user, project))
                self.assertEqual(caught.exception.reason, RefusalReason.TASK_ENDED)
                self.assertEqual(
                    [(e.reason, e.project_id) for e in self.refusals()],
                    [("task_ended", project)],
                )
        self.assertNothingRan()

    async def test_a_worker_of_a_run_that_a_restart_or_retry_replaced(self):
        for attempt, retry in ((2, 0), (1, 1), (3, 5)):
            with self.subTest(attempt=attempt, retry=retry):
                self.sink.events.clear()
                task = self.seed_task(self.user, attempt=attempt, retry_count=retry)
                project = self.project_of(task)
                with self.assertRaises(TaskNotUsableError) as caught:
                    await self.call(
                        context=self.context(task, self.user, project, TaskRun(1, 0))
                    )
                self.assertEqual(caught.exception.reason, RefusalReason.TASK_SUPERSEDED)
                self.assertEqual(
                    [e.reason for e in self.refusals()], ["task_superseded"]
                )
        self.assertNothingRan()

    async def test_the_current_run_of_a_restarted_task_may_call(self):
        task = self.seed_task(self.user, attempt=2, retry_count=3)
        await self.call(
            context=self.context(task, self.user, self.project_of(task), TaskRun(2, 3))
        )
        self.assertEqual(len(self.usage_rows()), 1)

    async def test_a_connection_that_is_not_usable_is_one_error_for_every_reason(self):
        messages = set()
        for label, prepare in (
            ("none", lambda: self.clear_connections()),
            ("disabled", lambda: self.replace_connection(enabled=False)),
            ("unavailable", lambda: self.replace_connection(status="unavailable")),
            ("expired", lambda: self.replace_connection(status="expired")),
        ):
            with self.subTest(label):
                self.sink.events.clear()
                prepare()
                with self.assertRaises(ConnectionUnavailableError) as caught:
                    await self.call()
                messages.add(str(caught.exception))
                self.assertRefusalAudited("connection_unavailable", project=True)
        self.assertEqual(messages, {"The connection is unavailable"})
        self.assertNothingRan()

    async def test_a_kind_without_an_adapter_is_unavailable_before_the_database(self):
        self.adapters._adapters.pop(CODEX)
        with self.assertRaises(ConnectionUnavailableError):
            await self.call()
        self.assertNothingRan()
        self.assertRefusalAudited("connection_unavailable", project=False)

    async def test_a_user_without_any_quota_may_not_start_a_new_task(self):
        stranger = self.seed_user()
        task = self.seed_task(stranger)
        with self.assertRaises(QuotaNotConfiguredError):
            await self.call(
                principal=self.principal(stranger),
                context=self.context(task, stranger, self.project_of(task)),
            )
        self.assertNothingRan()
        (event,) = self.refusals()
        self.assertEqual(
            (event.reason, event.actor_id), ("quota_not_configured", stranger)
        )

    async def test_a_principal_that_is_not_the_delegating_user_is_denied(self):
        other = self.seed_user()
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.call(principal=self.principal(other))
        self.assertEqual(caught.exception.reason, Reason.NOT_RESOURCE_OWNER)
        self.assertEqual(
            self.audit_actions(), [("agent.use", "deny", "not_resource_owner")]
        )
        self.assertNothingRan()

    async def test_an_owner_cannot_spend_another_users_task(self):
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.call(principal=self.principal(self.owner))
        self.assertEqual(caught.exception.reason, Reason.NOT_RESOURCE_OWNER)
        self.assertNothingRan()

    async def test_the_system_identity_may_not_use_the_connection(self):
        system = Principal(uuid.uuid4(), SystemRole.SYSTEM)
        task = self.seed_task(self.user)
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.call(
                principal=system,
                context=self.context(task, system.user_id, self.project_of(task)),
            )
        self.assertEqual(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertNothingRan()

    async def test_a_failing_authorizer_audit_stops_the_call(self):
        service = self.new_service(authorizer_sink=FailingSink())
        self.service = service
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.call()
        self.assertEqual(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertNothingRan()

    async def test_a_refusal_stands_when_its_own_audit_cannot_be_written(self):
        self.service = self.new_service(audit_sink=FailingSink())
        self.replace_connection(enabled=False)
        with self.assertLogs("paw_backend.connections", logging.ERROR) as logs:
            with self.assertRaises(ConnectionUnavailableError):
                await self.call()
        self.assertNotIn(CANARY, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNothingRan()

    async def test_a_refusal_leaves_the_quota_untouched(self):
        self.replace_connection(enabled=False)
        for _ in range(3):
            with self.assertRaises(ConnectionUnavailableError):
                await self.call()
        self.replace_connection(enabled=True)
        await self.call()
        self.assertEqual(len(self.usage_rows()), 1)

    # -- helpers ---------------------------------------------------------

    def replace_connection(self, **columns):
        self.clear_connections()
        self.seed_connection(CODEX)
        for name, value in columns.items():
            with self.engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE shared_connections SET {name} = :v"), {"v": value}
                )


if __name__ == "__main__":
    unittest.main()
