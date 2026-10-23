"""The late collection of a Codex session's login (issue #280, Decision 0096,
1 D, 2's fencing (b), 3 and 5), without a database: fake units, a fake
connection service, a real session directory.

* while the unit runs (or its cgroup is populated, or it has a job) nothing
  is read, written or deleted, but the fence is set at once (``launch.json``
  gone, the directory and ``config`` 0550) and its stop is tried;
* once stopped: the login is read back and handed over, the unit is checked
  once more, the directory becomes an empty 0550 tombstone, and only then is
  the slot given back with the result;
* no login (``missing``), not a login (``invalid``): the slot is given back
  with that result (the service marks the connection); a credential behind
  the row's version, or a unit that runs again after the read, keeps the
  slot; a transient failure keeps it for the next round;
* a fenced directory refuses a new start (``launch.json`` cannot be written);
* ``SystemdSessionUnits`` reads ``systemctl show`` and the unit's
  ``cgroup.events`` as Decision 0096, 3 says, and counts the sessions whose
  egress units outlive their CLI unit;
* nothing of the login is in the log.
"""

import asyncio
import dataclasses
import os
import shutil
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from paw_backend.agents import codex_collect
from paw_backend.agents.cli_session import SystemdLauncher
from paw_backend.agents.codex_collect import (
    FENCE_MODE,
    CodexCollectionLoop,
    CodexLoginCollector,
    SystemdSessionUnits,
)
from paw_backend.connections import ConnectionKind, SessionCollection
from paw_backend.connections.session_slot import keep_until_collected
from paw_backend.connections.store import SessionLease

CODEX = ConnectionKind.CODEX
HANDLE = "cred_" + "b" * 32
PLACED = '{"tokens": {"refresh_token": "fir' + 'st"}}'
REFRESHED = '{"tokens": {"refresh_token": "can' + 'ary-second"}}'


class Units:
    def __init__(self):
        self.running = True
        self.populated = False
        self.stops = 0
        self.egress = False
        self.after_read = None  # a state to switch to after the first check

    async def stopped(self, session):
        return not self.running and not self.populated

    async def stop(self, session):
        self.stops += 1
        raise OSError("polkit says no")

    async def egress_left(self, session):
        return self.egress


class Host:
    def __init__(self, lease, *, result=SessionCollection.WRITTEN):
        self.lease = lease
        self.result = result
        self.written = []
        self.finished = []
        self.renewals = True
        self.fail = None
        self.finish_fail = None

    async def session_lease(self, kind, holder):
        if self.lease is None or holder != self.lease.holder:
            return None
        return self.lease

    async def renew_session(self, kind, holder):
        return self.renewals

    async def write_collected_login(self, kind, handle, expected_version, value):
        if self.fail is not None:
            raise self.fail
        self.written.append((handle, expected_version, value))
        return self.result

    async def record_collection(self, kind, holder, result):
        self.lease = dataclasses.replace(self.lease, collected=result)
        return True

    async def finish_session(self, kind, holder, result):
        if self.finish_fail is not None:
            error, self.finish_fail = self.finish_fail, None
            raise error
        self.finished.append(result)
        return True


class CollectorCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(self.remove, self.base)
        self.session = uuid.uuid4()
        self.holder = uuid.uuid4()
        self.directory = self.base / str(self.session)
        self.directory.mkdir(mode=0o750)
        (self.directory / "config").mkdir(mode=0o750)
        (self.directory / "launch.json").write_text("{}")
        (self.directory / "bridge.token").write_text("t")
        (self.directory / "config" / "config.toml").write_text("")
        self.auth = self.directory / "config" / "auth.json"
        self.auth.write_text(REFRESHED)
        self.units = Units()
        self.host = Host(
            SessionLease(CODEX, self.holder, self.session, HANDLE, 2, True, False)
        )
        self.collector = CodexLoginCollector(
            self.host, self.units, session_base=self.base
        )

    @staticmethod
    def remove(path):
        for root, dirs, _ in os.walk(path):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o700)
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    async def collect(self):
        return await self.collector.collect(CODEX, self.holder)


class WaitTest(CollectorCase):
    async def test_a_running_unit_is_fenced_and_waited_for(self):
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR") as logs:
            self.assertFalse(await self.collect())
        self.assertFalse((self.directory / "launch.json").exists())
        self.assertEqual(self.mode(self.directory), FENCE_MODE)
        self.assertEqual(self.mode(self.directory / "config"), FENCE_MODE)
        self.assertEqual(self.auth.read_text(), REFRESHED)  # untouched
        self.assertEqual(self.units.stops, 1)  # its stop was tried
        self.assertEqual((self.host.written, self.host.finished), ([], []))
        self.assertIn(f"paw-cli-session@{self.session}.service", logs.output[0])
        # The CLI can still rewrite its login in place (Decision 0096, 2's 1).
        self.auth.write_text(REFRESHED)

    async def test_an_inactive_unit_with_a_populated_cgroup_is_waited_for(self):
        self.units.running = False
        self.units.populated = True
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertFalse(await self.collect())
        self.assertEqual(self.host.written, [])
        self.assertTrue(self.auth.exists())

    @unittest.skipIf(os.geteuid() == 0, "root ignores the directory's mode")
    async def test_a_fenced_directory_refuses_a_new_start(self):
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            await self.collect()
        launcher = SystemdLauncher(systemctl=("/bin/false",))

        class Spec:
            directory = self.directory

        with self.assertRaises(OSError):
            await launcher.start_interactive(["codex"], {}, Spec())
        self.assertFalse((self.directory / "launch.json").exists())


class FenceTest(CollectorCase):
    """Codex review of #292 (P1): creation is blocked before the launch file
    is looked at; a launch file that survived is removed with the directory
    writable for that instant only; one that keeps coming back keeps the
    slot instead of wedging every later fence."""

    async def test_creation_is_blocked_before_the_launch_file_is_removed(self):
        modes = []
        remove = codex_collect._remove

        def removing(path):
            modes.append(
                (self.mode(self.directory / "config"), self.mode(self.directory))
            )
            remove(path)

        with patch.object(codex_collect, "_remove", removing):
            self.assertTrue(codex_collect.fence(self.directory))
        # ``config`` was already closed; the directory was opened only for
        # the removal, keeping the CLI's group's traversal (Codex review of
        # #292: the unit may still run and save a refreshed login), and is
        # closed again.
        self.assertEqual(modes, [(FENCE_MODE, 0o750)])
        self.assertFalse((self.directory / "launch.json").exists())
        self.assertEqual(self.mode(self.directory), FENCE_MODE)

    async def test_the_group_can_reach_the_login_throughout_the_fence(self):
        # Every mode the directory has during the fence keeps the group's x.
        seen = []
        chmod = os.chmod

        def recording(path, mode, *args, **kwargs):
            if Path(path) == self.directory:
                seen.append(mode)
            return chmod(path, mode, *args, **kwargs)

        with patch.object(codex_collect.os, "chmod", recording):
            self.assertTrue(codex_collect.fence(self.directory))
        self.assertTrue(seen)
        for mode in seen:
            self.assertTrue(mode & stat.S_IXGRP, oct(mode))

    async def test_a_launch_file_in_an_already_fenced_directory_is_removed(self):
        # A previous round fenced the directory; a launch file is there all
        # the same (written just before it): no later fence is wedged.
        os.chmod(self.directory, FENCE_MODE)
        self.assertTrue(codex_collect.fence(self.directory))
        self.assertFalse((self.directory / "launch.json").exists())
        self.assertEqual(self.mode(self.directory), FENCE_MODE)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the directory's mode")
    async def test_nothing_can_be_created_once_fenced(self):
        self.assertTrue(codex_collect.fence(self.directory))
        for path in (self.directory / "launch.json", self.directory / "config" / "x"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                path.write_text("{}")

    async def test_a_launch_file_that_keeps_coming_back_keeps_the_slot(self):
        self.units.running = False
        with (
            patch.object(codex_collect, "_remove", lambda path: None),
            self.assertLogs("paw_backend.agents.codex_collect", "ERROR") as logs,
        ):
            self.assertFalse(await self.collect())
        self.assertIn("came back", "\n".join(logs.output))
        self.assertEqual((self.host.written, self.host.finished), ([], []))
        self.assertEqual(self.auth.read_text(), REFRESHED)
        self.assertEqual(self.mode(self.directory), FENCE_MODE)
        # Once it stays away, the next round collects.
        self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.WRITTEN])


class CollectTest(CollectorCase):
    async def test_a_failed_finish_is_retried_with_the_kept_result(self):
        # Codex review of #292 (P2): the definite result is on the row before
        # the login goes, so the next round does not find "no login".
        self.units.running = False
        self.host.finish_fail = TimeoutError()
        with self.assertLogs("paw_backend.agents.codex_collect", "WARNING"):
            self.assertFalse(await self.collect())
        self.assertEqual(self.host.lease.collected, SessionCollection.WRITTEN)
        self.assertEqual(list(self.directory.iterdir()), [])  # the tombstone
        self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.WRITTEN])
        self.assertEqual(len(self.host.written), 1)  # written once

    async def test_a_stopped_unit_is_collected_and_tombstoned(self):
        self.units.running = False
        with self.assertLogs("paw_backend.agents.codex_collect", "INFO") as logs:
            self.assertTrue(await self.collect())
        self.assertEqual(self.host.written, [(HANDLE, 2, REFRESHED)])
        self.assertEqual(self.host.finished, [SessionCollection.WRITTEN])
        self.assertTrue(self.directory.is_dir())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.mode(self.directory), FENCE_MODE)
        self.assertNotIn("canary", "\n".join(logs.output))

    async def test_no_login_is_missing_and_not_a_login_is_invalid(self):
        self.units.running = False
        self.auth.unlink()
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.MISSING])
        self.assertEqual(self.host.written, [])

    async def test_not_a_login_is_invalid(self):
        self.units.running = False
        self.auth.write_text("[1, 2]")
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.INVALID])

    async def test_a_directory_gone_with_a_reboot_is_missing(self):
        self.units.running = False
        self.remove(self.directory)
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.MISSING])

    async def test_a_credential_behind_the_row_keeps_the_slot(self):
        self.units.running = False
        self.host.result = None
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertFalse(await self.collect())
        self.assertEqual(self.host.finished, [])
        self.assertTrue(self.auth.exists())

    async def test_a_unit_that_runs_again_after_the_read_keeps_the_slot(self):
        self.units.running = False
        write = self.host.write_collected_login

        async def started_again(*args):
            self.units.running = True
            return await write(*args)

        self.host.write_collected_login = started_again
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR"):
            self.assertFalse(await self.collect())
        self.assertEqual(self.host.finished, [])
        self.assertTrue(self.auth.exists())

    async def test_a_transient_failure_is_tried_again(self):
        self.units.running = False
        self.host.fail = TimeoutError()
        with self.assertLogs("paw_backend.agents.codex_collect", "WARNING"):
            self.assertFalse(await self.collect())
        self.assertTrue(self.auth.exists())
        self.host.fail = None
        self.assertTrue(await self.collect())
        self.assertEqual(self.host.finished, [SessionCollection.WRITTEN])

    async def test_a_slot_taken_over_meanwhile_is_left(self):
        self.units.running = False
        self.host.renewals = False
        self.assertTrue(await self.collect())
        self.assertEqual((self.host.written, self.host.finished), ([], []))
        self.assertTrue(self.auth.exists())

    async def test_a_slot_that_is_not_this_holders_is_nothing_to_do(self):
        self.host.lease = None
        self.assertTrue(await self.collect())
        self.assertTrue((self.directory / "launch.json").exists())

    async def test_egress_units_left_are_logged_but_do_not_hold_it_up(self):
        self.units.running = False
        self.units.egress = True
        with self.assertLogs("paw_backend.agents.codex_collect", "ERROR") as logs:
            self.assertTrue(await self.collect())
        self.assertIn(
            f"paw-egress-session@{self.session}.socket", "\n".join(logs.output)
        )


class FakeSystemctl:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def systemctl(self, *arguments):
        self.calls.append(arguments)
        return self.answers(arguments)

    async def query(self, *arguments, timeout_seconds):
        return await self.systemctl(*arguments)

    async def stop_unit(self, unit, attempts=3):
        self.calls.append(("stop", unit, attempts))


class SystemdUnitsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.session = uuid.uuid4()
        self.unit = f"paw-cli-session@{self.session}.service"

    def units(self, show):
        def answers(arguments):
            return show.get(arguments[-1], (1, ""))

        self.systemctl = FakeSystemctl(answers)
        return SystemdSessionUnits(
            self.systemctl,
            cgroup_root=self.root,
            egress_socket_dir=self.root / "egress",
        )

    def cgroup(self, populated):
        path = self.root / self.unit
        path.mkdir()
        (path / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")

    async def test_stopped_needs_an_ended_state_no_job_and_an_empty_cgroup(self):
        cases = [
            ((0, "ActiveState=inactive\nJob="), None, True),
            ((0, "ActiveState=failed\nJob="), "0", True),
            ((0, "ActiveState=inactive\nJob="), "1", False),
            ((0, "ActiveState=active\nJob="), None, False),
            ((0, "ActiveState=deactivating\nJob=7"), None, False),
            ((0, "ActiveState=inactive\nJob=12"), None, False),
            ((1, ""), None, False),  # systemctl failed: not known
            ((0, "ActiveState=inactive"), None, False),  # Job not shown
        ]
        for show, populated, expected in cases:
            with self.subTest(show=show, populated=populated):
                shutil.rmtree(self.root / self.unit, ignore_errors=True)
                if populated is not None:
                    self.cgroup(populated)
                units = self.units({self.unit: show})
                self.assertIs(await units.stopped(self.session), expected)
        self.assertEqual(
            self.systemctl.calls[0],
            ("show", "--property", "ActiveState,Job", self.unit),
        )

    async def test_a_cgroup_without_its_events_is_not_known_empty(self):
        (self.root / self.unit).mkdir()
        units = self.units({self.unit: (0, "ActiveState=inactive\nJob=")})
        self.assertFalse(await units.stopped(self.session))

    async def test_the_stop_is_the_session_units_one_try(self):
        units = self.units({})
        await units.stop(self.session)
        self.assertEqual(self.systemctl.calls, [("stop", self.unit, 1)])

    async def test_egress_left(self):
        socket = f"paw-egress-session@{self.session}.socket"
        units = self.units({socket: (0, "ActiveState=active")})
        self.assertTrue(await units.egress_left(self.session))
        units = self.units({socket: (0, "ActiveState=inactive")})
        self.assertFalse(await units.egress_left(self.session))
        (self.root / "egress").mkdir()
        (self.root / "egress" / f"{self.session}.sock").write_text("")
        self.assertTrue(await units.egress_left(self.session))

    async def test_egress_left_over_counts_sessions_without_their_cli(self):
        alive, gone = uuid.uuid4(), uuid.uuid4()
        listing = "\n".join(
            [
                f"paw-egress-session@{alive}.socket loaded active listening x",
                f"paw-egress-session@{alive}.service loaded active running x",
                f"paw-cli-session@{alive}.service loaded active running x",
                f"paw-egress-session@{gone}.socket loaded active listening x",
                f"paw-cli-session@{gone}.service loaded inactive dead x",
                f"paw-egress-session@{uuid.uuid4()}.socket loaded inactive dead x",
            ]
        )

        def answers(arguments):
            return (0, listing) if arguments[0] == "list-units" else (1, "")

        units = SystemdSessionUnits(FakeSystemctl(answers), cgroup_root=self.root)
        self.assertEqual(await units.egress_left_over(), 1)
        failing = SystemdSessionUnits(
            FakeSystemctl(lambda arguments: (1, "")), cgroup_root=self.root
        )
        self.assertIsNone(await failing.egress_left_over())


class HungSystemctlProbeTest(unittest.IsolatedAsyncioTestCase):
    """Codex review of #292 (P2): System Health's look at the egress units
    stays within the check's deadline when systemctl (D-Bus) hangs: bounded
    by its own short time, and a cancellation is not held for the
    launcher's ``systemctl_seconds`` (a read-only look sends nothing)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        hung = self.root / "systemctl"
        hung.write_text("#!/bin/sh\nexec sleep 60\n")
        hung.chmod(0o755)
        # The launcher's bound is far past the health check's 5 seconds.
        self.launcher = SystemdLauncher(systemctl=(str(hung),), systemctl_seconds=30)

    async def test_a_hung_systemctl_is_not_known_within_the_probe_bound(self):
        units = SystemdSessionUnits(
            self.launcher, cgroup_root=self.root, probe_seconds=0.5
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertLogs("paw_backend.agents", "WARNING"):
            left = await asyncio.wait_for(units.egress_left_over(), 5)
        self.assertIsNone(left)
        self.assertLess(loop.time() - started, 3)

    async def test_a_cancelled_probe_is_not_held(self):
        units = SystemdSessionUnits(
            self.launcher, cgroup_root=self.root, probe_seconds=30
        )
        probing = asyncio.ensure_future(units.egress_left_over())
        await asyncio.sleep(0.3)  # systemctl list-units runs (and hangs)
        probing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(probing), 3)


class KeepTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_renews_and_collects_until_done(self):
        renewals, rounds = [], []

        async def renew():
            renewals.append(1)
            return True

        async def collect():
            rounds.append(1)
            return len(rounds) == 3

        await asyncio.wait_for(
            keep_until_collected(
                renew,
                collect,
                heartbeat_seconds=0.01,
                interval_seconds=0.05,
                first_at_once=True,
            ),
            5,
        )
        self.assertEqual(len(rounds), 3)
        self.assertGreater(len(renewals), 3)

    async def test_it_ends_once_the_slot_is_lost(self):
        async def renew():
            return False

        async def collect():
            raise AssertionError("not after the slot was lost")

        await asyncio.wait_for(
            keep_until_collected(
                renew,
                collect,
                heartbeat_seconds=0.01,
                interval_seconds=1,
                first_at_once=False,
            ),
            5,
        )

    async def test_a_failing_renewal_or_round_is_tried_again(self):
        calls = []

        async def renew():
            calls.append("renew")
            if calls.count("renew") == 1:
                raise TimeoutError()
            return True

        async def collect():
            calls.append("collect")
            if calls.count("collect") == 1:
                raise RuntimeError("boom")
            return True

        await asyncio.wait_for(
            keep_until_collected(
                renew,
                collect,
                heartbeat_seconds=0.01,
                interval_seconds=0.02,
                first_at_once=True,
            ),
            5,
        )
        self.assertEqual(calls.count("collect"), 2)


class LoopTest(unittest.IsolatedAsyncioTestCase):
    """The loop takes over expired bound slots only (Decision 0096, 1 D)."""

    async def test_only_expired_bound_slots_are_taken_over(self):
        expired = SessionLease(
            CODEX, uuid.uuid4(), uuid.uuid4(), HANDLE, 1, False, True
        )
        alive = SessionLease(
            ConnectionKind.CLAUDE, uuid.uuid4(), uuid.uuid4(), HANDLE, 1, True, False
        )
        taken = []
        collected = asyncio.Event()

        class LoopHost:
            async def session_leases(self):
                return (expired, alive)

            async def take_over_session(self, kind, previous, holder):
                taken.append((kind, previous))
                return True

            async def renew_session(self, kind, holder):
                return True

        class Collector:
            async def collect(self, kind, holder):
                collected.set()
                return True

        loop = CodexCollectionLoop(
            LoopHost(), Collector(), interval_seconds=0.05, heartbeat_seconds=0.01
        )
        running = asyncio.ensure_future(loop.run())
        await asyncio.wait_for(collected.wait(), 5)
        loop.stop()
        await asyncio.wait_for(running, 5)
        self.assertEqual(taken[0], (CODEX, expired.holder))
        self.assertNotIn((ConnectionKind.CLAUDE, alive.holder), taken)


if __name__ == "__main__":
    unittest.main()
