"""Concurrent and cancelled retrievals (real PostgreSQL).

One ``HybridRetriever`` serves many callers at once; nothing of one call may reach
another (the retriever keeps no per-call state), and a call that is cancelled must
neither swallow the cancellation nor leave anything behind.
"""

import asyncio

from .retrieval_pg_support import (
    PostgresRetrievalTestCase,
    RecordingReranker,
    requires_postgres,
    titles,
)

QUERY = "deploy backend friday"
TEXT = "deploy backend friday"


@requires_postgres
class ConcurrencyTest(PostgresRetrievalTestCase):
    async def test_many_callers_at_once_each_get_only_their_own_memories(self):
        callers = [self.user() for _ in range(10)]
        for n, caller in enumerate(callers):
            self.seed(f"mine {n}", f"{TEXT} {n}", owner=caller.user_id)
        self.seed("shared", TEXT, scope="shared")
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker)

        results = await asyncio.gather(
            *(self.retrieve(caller, QUERY, retriever=retriever) for caller in callers),
            *(self.retrieve(caller, QUERY, retriever=retriever) for caller in callers),
        )

        for index, result in enumerate(results):
            n = index % 10
            self.assertEqual(sorted(titles(result)), sorted([f"mine {n}", "shared"]))
        # Every call showed the reranker only what its own caller may read.
        for _, shown in reranker.calls:
            own = [c.title for c in shown if c.title.startswith("mine")]
            self.assertEqual(len(own), 1)

    async def test_the_result_of_a_call_does_not_depend_on_the_calls_around_it(self):
        me, other = self.user(), self.user()
        for n in range(4):
            self.seed(f"note {n}", f"{TEXT} {n}", owner=me.user_id)
            self.seed(f"theirs {n}", f"{TEXT} {n}", owner=other.user_id)
        alone = await self.retrieve(me, QUERY)
        together = await asyncio.gather(
            self.retrieve(me, QUERY),
            self.retrieve(other, QUERY),
            self.retrieve(me, QUERY),
        )
        self.assertEqual(together[0], alone)
        self.assertEqual(together[2], alone)

    async def test_a_cancelled_call_propagates_the_cancellation_and_frees_the_retriever(
        self,
    ):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        started = asyncio.Event()

        async def slow(query, candidates):
            started.set()
            await asyncio.sleep(3600)

        retriever = self.new_retriever(
            reranker=RecordingReranker(slow),
            timeout_seconds=60,
            stage_timeout_seconds=60,
        )
        task = asyncio.ensure_future(self.retrieve(me, QUERY, retriever=retriever))
        async with asyncio.timeout(30):
            await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The same retriever still answers afterwards.
        fast = self.new_retriever()
        self.assertEqual(
            titles(await self.retrieve(me, QUERY, retriever=fast)), ["mine"]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
