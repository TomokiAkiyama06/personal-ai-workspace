"""The late collection of a Codex session's login end to end (issue #280,
Decision 0096's tests 1-8 and 10): real PostgreSQL, a real
``FileSecretStore``, a real session directory, fake units.

* a kept session (its CLI could not be stopped, its login written back at
  once as version 2) is collected by the process that keeps it once the unit
  stopped: the CLI's later refresh becomes version 3, the directory an empty
  tombstone, the slot is given back (``connection.session_collected``
  ``written``) and the next session runs with the refreshed login;
* a bound slot whose process is gone is taken over by the collection loop
  of another process (one of two loops wins) and collected the same way;
* a row whose expected version is behind the store (the process died between
  the write-back and the row's update) is collected against the store's
  current version; a replaced credential is ``detached`` and left alone;
* a session without its login (a reboot) is ``missing``: the connection is
  marked and System Health reports ``collect_failed`` until a health check
  finds it ``connected``;
* the loop leaves unbound and unexpired rows, and other directories, alone.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import text

from paw_backend.agents.codex_collect import CodexCollectionLoop, CodexLoginCollector
from paw_backend.connections import ConnectionKind, ConnectionStatus
from paw_backend.db import Database
from paw_backend.health.domain import Severity
from paw_backend.health.sources import ConnectionSource

from .support import make_settings
from .task_support import TEST_DATABASE_URL
from .test_connections_session_binding import BindingSession
from .test_connections_session_slot import LOGIN, REFRESHED, ExclusiveSession, SlotCase

CODEX = ConnectionKind.CODEX
LATER = '{"tokens": {"refresh_token": "third"}}'
# Fast collection rounds for the tests (production: 60 s).
FAST_COLLECT = {"session_collect_interval_seconds": 0.2}


class Units:
    def __init__(self):
        self.running = True

    async def stopped(self, session):
        return not self.running

    async def stop(self, session):
        raise OSError("still running")

    async def egress_left(self, session):
        return False


class CollectionCase(SlotCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(self.remove, self.base)
        self.units = Units()
        self.addAsyncCleanup(self.let_holds_go)

    async def let_holds_go(self):
        # Kept and taken-over slots stop once their rows are gone: before this
        # test's loop closes, so no renewal is left half-way for the next test.
        await asyncio.to_thread(self.clean_leases)
        await asyncio.sleep(0.3)

    @staticmethod
    def remove(path):
        for root, dirs, _ in os.walk(path):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o700)
        shutil.rmtree(path, ignore_errors=True)

    def slot_service(self, adapter, **options):
        # A longer lease than the slot tests' (a slow database must not let a
        # kept or taken-over slot expire between renewals).
        return super().slot_service(
            adapter,
            **{
                "session_slot_ttl_seconds": 6.0,
                "session_slot_margin_seconds": 3.0,
                **options,
            },
        )

    def collecting_service(self, adapter, **options):
        service = self.slot_service(adapter, **{**FAST_COLLECT, **options})
        service.attach_session_collector(
            CodexLoginCollector(service, self.units, session_base=self.base)
        )
        return service

    def session_directory(self, session, login):
        directory = self.base / str(session)
        (directory / "config").mkdir(parents=True)
        (directory / "launch.json").write_text("{}")
        (directory / "config" / "auth.json").write_text(login)
        return directory

    def collected(self):
        return [
            e.reason
            for e in self.sink.events
            if e.action == "connection.session_collected"
        ]

    async def until(self, check, seconds=10):
        for _ in range(int(seconds / 0.05)):
            if check():
                return
            await asyncio.sleep(0.05)
        self.fail("not reached")


class KeptTest(CollectionCase):
    async def test_a_kept_session_is_collected_once_its_unit_stopped(self):
        adapter = BindingSession(stuck=True, refresh=REFRESHED)
        service = self.collecting_service(adapter)
        directory = self.session_directory(adapter.session, REFRESHED)
        await self.start(service)
        self.assertEqual((await self.store.read(self.handle)).version, 2)
        # The CLI refreshes again after the immediate read-back.
        (directory / "config" / "auth.json").write_text(LATER)
        await asyncio.sleep(0.6)
        # Running: nothing collected, the fence is set, the slot is kept.
        self.assertEqual(self.collected(), [])
        self.assertFalse((directory / "launch.json").exists())
        self.assertEqual(len(self.leases()), 1)
        self.units.running = False
        await self.until(lambda: self.collected())
        self.assertEqual(self.collected(), ["written"])
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (3, LATER))
        self.assertEqual(self.leases(), [])
        self.assertEqual(list(directory.iterdir()), [])
        after = ExclusiveSession()
        await self.start(self.slot_service(after))
        self.assertEqual(after.secrets, [LATER])

    async def test_a_failed_finish_does_not_turn_into_missing(self):
        # Codex review of #292 (P2): the result is kept on the row before the
        # login is deleted; the round after a failure to give the slot back
        # finishes with it (no mark on the connection).
        adapter = BindingSession(stuck=True)
        directory = self.session_directory(adapter.session, REFRESHED)
        service = self.collecting_service(adapter)
        finish = service.finish_session
        failures = [TimeoutError()]

        async def failing_once(*args):
            if failures:
                raise failures.pop()
            return await finish(*args)

        service.finish_session = failing_once
        self.units.running = False
        await self.start(service)
        await self.until(lambda: self.collected())
        self.assertEqual(self.collected(), ["written"])
        self.assertEqual(failures, [])
        self.assertEqual(list(directory.iterdir()), [])
        (row,) = self.rows(
            "SELECT suspect_reason FROM shared_connections WHERE kind = 'codex'"
        )
        self.assertIsNone(row["suspect_reason"])
        self.assertEqual((await self.store.read(self.handle)).version, 2)

    async def test_an_unchanged_login_is_given_back_unchanged(self):
        adapter = BindingSession(stuck=True)
        self.session_directory(adapter.session, LOGIN)
        self.units.running = False
        await self.start(self.collecting_service(adapter))
        await self.until(lambda: self.collected())
        self.assertEqual(self.collected(), ["unchanged"])
        self.assertEqual((await self.store.read(self.handle)).version, 1)


class LoopCase(CollectionCase):
    def orphan(self, session, version=1, handle=None):
        """The row a process that died after binding leaves."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_session_leases (kind, holder,"
                    " acquired_at, heartbeat_at, expires_at, session,"
                    " secret_handle, expected_version) VALUES ('codex', :holder,"
                    " now() - interval '1 hour', now() - interval '1 hour',"
                    " now() - interval '1 minute', :session, :handle, :version)"
                ),
                {
                    "holder": uuid.uuid4(),
                    "session": session,
                    "handle": handle or self.handle,
                    "version": version,
                },
            )

    def loop(self, service):
        return CodexCollectionLoop(
            service,
            CodexLoginCollector(service, self.units, session_base=self.base),
            interval_seconds=0.1,
            heartbeat_seconds=0.05,
        )

    async def running(self, *loops):
        tasks = [asyncio.ensure_future(loop.run()) for loop in loops]

        async def stop():
            for loop in loops:
                loop.stop()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)

        self.addAsyncCleanup(stop)


class TakeOverTest(LoopCase):
    async def test_a_gone_process_is_taken_over_by_one_loop_and_collected(self):
        session = uuid.uuid4()
        directory = self.session_directory(session, REFRESHED)
        self.orphan(session)
        self.units.running = False
        first = self.loop(self.slot_service(ExclusiveSession()))
        second = self.loop(self.slot_service(ExclusiveSession()))
        await self.running(first, second)
        await self.until(lambda: self.collected())
        await asyncio.sleep(0.3)
        self.assertEqual(self.collected(), ["written"])
        self.assertEqual((await self.store.read(self.handle)).version, 2)
        self.assertEqual(self.leases(), [])
        self.assertEqual(list(directory.iterdir()), [])

    async def test_the_expected_version_is_the_stores_when_the_row_is_behind(self):
        # The immediate write-back stored version 2; the process died before
        # the row followed (Decision 0096, 2's version check).
        await self.store.rotate(self.handle, CODEX, 1, REFRESHED)
        session = uuid.uuid4()
        self.session_directory(session, LATER)
        self.orphan(session, version=1)
        self.units.running = False
        await self.running(self.loop(self.slot_service(ExclusiveSession())))
        await self.until(lambda: self.collected())
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (3, LATER))

    async def test_a_replaced_credential_is_detached_and_left_alone(self):
        session = uuid.uuid4()
        self.session_directory(session, LATER)
        self.orphan(session)
        replacement = await self.store.put(CODEX, LOGIN)
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE shared_connections SET secret_handle = :h"),
                {"h": replacement},
            )
        self.units.running = False
        await self.running(self.loop(self.slot_service(ExclusiveSession())))
        await self.until(lambda: self.collected())
        self.assertEqual(self.collected(), ["detached"])
        self.assertEqual((await self.store.read(self.handle)).version, 1)
        self.assertEqual((await self.store.read(replacement)).version, 1)

    async def test_a_session_without_its_login_marks_the_connection(self):
        session = uuid.uuid4()  # its directory went with a reboot
        self.orphan(session)
        self.units.running = False
        await self.running(self.loop(self.slot_service(ExclusiveSession())))
        await self.until(lambda: self.collected())
        self.assertEqual(self.collected(), ["missing"])
        self.assertEqual(self.leases(), [])
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        health = await ConnectionSource(database).check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertIn("collect_failed:codex", health.reasons)
        # The next working health check clears it.
        await self.slot_service(ExclusiveSession())._store.record_health(
            CODEX, self.handle, ConnectionStatus.CONNECTED
        )
        health = await ConnectionSource(database).check()
        self.assertNotIn("collect_failed:codex", health.reasons)

    async def test_unexpired_and_unbound_rows_and_other_directories_stay(self):
        other = self.session_directory(uuid.uuid4(), LOGIN)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_session_leases (kind, holder,"
                    " acquired_at, heartbeat_at, expires_at, session,"
                    " secret_handle, expected_version) VALUES ('codex', :holder,"
                    " now(), now(), now() + interval '1 hour', :session, :h, 1),"
                    " ('claude', :holder, now() - interval '1 hour',"
                    " now() - interval '1 hour', now() - interval '1 minute',"
                    " NULL, NULL, NULL)"
                ),
                {"holder": uuid.uuid4(), "session": uuid.uuid4(), "h": self.handle},
            )
        before = self.rows("SELECT kind, holder FROM connection_session_leases")
        self.units.running = False
        await self.running(self.loop(self.slot_service(ExclusiveSession())))
        await asyncio.sleep(0.5)
        self.assertEqual(
            self.rows("SELECT kind, holder FROM connection_session_leases"), before
        )
        self.assertTrue((other / "launch.json").exists())
        self.assertEqual(self.collected(), [])
