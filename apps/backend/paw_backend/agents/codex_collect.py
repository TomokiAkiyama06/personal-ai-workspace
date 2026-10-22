"""The late collection of a Codex session's login (issue #280, Decision 0096).

A Codex CLI that could not be stopped (``UnitNotStoppedError``), or whose
backend process ended before it did, may refresh its ChatGPT login (a
single-use refresh token) after the session's immediate read-back. Its
connection's session slot stays bound to it (``connections.session_slot``,
revision ``0280``) until :class:`CodexLoginCollector` collected that login
after the session's unit was confirmed stopped:

* who looks (Decision 0096, 1 D): the process that keeps the slot, every
  ``COLLECT_INTERVAL_SECONDS`` (60 s, provisional), and in every backend
  process :class:`CodexCollectionLoop`, which takes over (compare-and-swap on
  the holder) a bound slot whose lease expired (its process is gone) and
  keeps it until collected. It looks at the slots' rows only, never at the
  directories under ``/run/paw-cli`` (another process's running sessions).
* the order (Decision 0096, 2's fencing (b)): (1) the fence: the session's
  ``launch.json`` is deleted and its directory and ``config`` made 0550
  (nothing can be created, deleted or renamed in them, so neither the unit's
  program nor a resumed previous holder can start the session again; the
  CLI's group can still reach ``auth.json`` and rewrite it in place);
  (2) the unit is confirmed stopped (otherwise its stop is tried, and the
  collection waits for the next round); (3) ``auth.json`` is read back and,
  when it differs from the credential's current version, written through
  ``ConnectionService.write_collected_login`` (compare-and-swap against the
  current version, which is at least the row's: Decision 0096, 2's version
  check; with the row's handle: a replaced credential is ``detached``);
  (4) the unit is confirmed stopped again; (5) the directory's content is
  deleted (made 0700 for that instant only, then 0550 again: an empty
  tombstone that ``/run``'s tmpfs drops at the next boot), and only once it is
  empty and the unit is still stopped is the slot given back
  (``ConnectionService.finish_session``: the row goes, a login that could not
  be read marks the connection, ``connection.session_collected`` audited).
* stopped (Decision 0096, 3): ``paw-cli-session@<uuid>.service`` is
  ``inactive`` or ``failed`` with no job, AND its cgroup is empty (gone, or
  ``populated 0``: the container's cgroup is below the unit's, so nothing that
  could write ``auth.json`` is left). The session's egress units
  (``paw-egress-session@<uuid>.socket`` / ``.service``) and socket are looked
  at too: left over they are an ERROR in the log (the backend cannot stop
  them; System Health reads them as well), not a condition of the collection.
* a failure (Decision 0096, 5): a transient one (the database, the secret
  store, ``systemctl``, a file operation) keeps the slot and the directory for
  the next round; a definite result (written, unchanged, a compare-and-swap
  that cannot write, no login, not a login) ends the collection.

Nothing of the login is logged: only the session's UUID (so the operator can
stop its unit), the steps and the result's code.
"""

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Final, Protocol

from paw_backend.agents.cli_session import CONFIG_DIR_NAME, SystemdLauncher
from paw_backend.agents.codex_credentials import AUTH_NAME, read_login_file
from paw_backend.connections import ConnectionKind, SessionCollection
from paw_backend.connections.session_slot import (
    SLOT_HEARTBEAT_SECONDS,
    SessionCollector,
    keep_until_collected,
)
from paw_backend.research.providers.broker import log_type_name

logger = logging.getLogger(__name__)

#: Decision 0096, 1 D: how often a kept slot's unit is looked at and the
#: collection loop runs (provisional).
COLLECT_INTERVAL_SECONDS: Final = 60.0
#: The slice of the session units (``paw-cli-session@.service``'s template).
CGROUP_ROOT: Final = Path(
    "/sys/fs/cgroup/system.slice/system-paw\\x2dcli\\x2dsession.slice"
)
#: Decision 0091: the session's egress socket (``RemoveOnStop=yes``).
EGRESS_SOCKET_DIR: Final = Path("/run/paw-egress/session")
#: Decision 0096, 2's fence and tombstone.
FENCE_MODE: Final = 0o550
_OPEN_MODE: Final = 0o700
_STOPPED_STATES: Final = frozenset({"inactive", "failed"})
LAUNCH_NAME: Final = "launch.json"


class SessionUnits(Protocol):
    """What the collection asks of systemd about one session."""

    async def stopped(self, session: uuid.UUID) -> bool: ...

    async def stop(self, session: uuid.UUID) -> None: ...

    async def egress_left(self, session: uuid.UUID) -> bool: ...


class Systemctl(Protocol):
    async def systemctl(self, *arguments: str) -> tuple[int, str]: ...

    async def stop_unit(self, unit: str, attempts: int = 3) -> None: ...


class SystemdSessionUnits:
    """Production's :class:`SessionUnits`: ``systemctl show`` (read only; polkit
    is not involved), the unit's ``cgroup.events`` (readable by the backend's
    user), and ``systemctl stop`` of the session's unit (the one polkit lets
    the backend's user stop). ``systemd`` is ``cli_session.SystemdLauncher``
    (its bounded ``systemctl``)."""

    def __init__(
        self,
        systemd: Systemctl,
        *,
        unit_prefix: str = "paw-cli-session@",
        egress_prefix: str = "paw-egress-session@",
        cgroup_root: Path = CGROUP_ROOT,
        egress_socket_dir: Path = EGRESS_SOCKET_DIR,
    ) -> None:
        self._systemd = systemd
        self._prefix = unit_prefix
        self._egress = egress_prefix
        self._cgroup_root = Path(cgroup_root)
        self._egress_sockets = Path(egress_socket_dir)

    def unit(self, session: uuid.UUID) -> str:
        return f"{self._prefix}{session}.service"

    async def _properties(self, unit: str, *names: str) -> dict[str, str] | None:
        code, out = await self._systemd.systemctl(
            "show", "--property", ",".join(names), unit
        )
        if code != 0:
            return None
        found = {}
        for line in out.splitlines():
            name, sep, value = line.partition("=")
            if sep:
                found[name.strip()] = value.strip()
        return found if all(name in found for name in names) else None

    async def stopped(self, session: uuid.UUID) -> bool:
        """Decision 0096, 3: ``inactive`` / ``failed``, no job, an empty
        cgroup. Anything not known is not stopped."""
        unit = self.unit(session)
        found = await self._properties(unit, "ActiveState", "Job")
        if found is None:
            return False
        if found["ActiveState"] not in _STOPPED_STATES:
            return False
        if found["Job"] not in ("", "0"):
            return False
        return await asyncio.to_thread(_cgroup_empty, self._cgroup_root / unit)

    async def stop(self, session: uuid.UUID) -> None:
        await self._systemd.stop_unit(self.unit(session), attempts=1)

    async def egress_left(self, session: uuid.UUID) -> bool:
        """The session's egress units still active, or its socket still
        there (Decision 0096, 3: logged, not a condition)."""
        for suffix in (".socket", ".service"):
            found = await self._properties(
                f"{self._egress}{session}{suffix}", "ActiveState"
            )
            if found is not None and found["ActiveState"] not in _STOPPED_STATES:
                return True
        return await asyncio.to_thread(
            os.path.lexists, self._egress_sockets / f"{session}.sock"
        )

    async def egress_left_over(self) -> int | None:
        """System Health's look (Decision 0096, 7): how many sessions have an
        egress unit active while their CLI unit is not. ``None``: systemd
        could not be asked."""
        try:
            code, out = await self._list_units()
        except OSError:
            return None  # no systemctl here
        if code != 0:
            return None
        return _left_over(out, self._egress, self._prefix)

    async def _list_units(self) -> tuple[int, str]:
        return await self._systemd.systemctl(
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            f"{self._egress}*",
            f"{self._prefix}*",
        )


def _left_over(listing: str, egress_prefix: str, cli_prefix: str) -> int:
    """How many sessions in ``systemctl list-units --plain`` output have an
    egress unit that has not ended while their CLI unit has."""
    egress: set[str] = set()
    running: set[str] = set()
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        name, active = fields[0], fields[2]
        if active in _STOPPED_STATES:
            continue
        instance = name.partition("@")[2].rpartition(".")[0]
        if name.startswith(egress_prefix):
            egress.add(instance)
        elif name.startswith(cli_prefix):
            running.add(instance)
    return len(egress - running)


def _cgroup_empty(path: Path) -> bool:
    try:
        text = (path / "cgroup.events").read_text(encoding="ascii")
    except FileNotFoundError:
        # No cgroup at all: nothing runs in it (a cgroup always has the file).
        return not os.path.lexists(path)
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == "populated":
            return value.strip() == "0"
    return False


class CollectionHost(Protocol):
    """The connection service's side of the collection (``ConnectionService``)."""

    async def session_lease(self, kind: ConnectionKind, holder: uuid.UUID): ...

    async def renew_session(self, kind: ConnectionKind, holder: uuid.UUID) -> bool: ...

    async def write_collected_login(
        self, kind: ConnectionKind, handle: str, expected_version: int, value: str
    ) -> SessionCollection | None: ...

    async def finish_session(
        self, kind: ConnectionKind, holder: uuid.UUID, result: SessionCollection
    ) -> bool: ...


class CodexLoginCollector:
    """One round of the collection of a kept session's login (see the
    module): :meth:`collect` is ``True`` once the slot was given back (or is
    no longer this holder's), ``False`` when it must be tried again."""

    def __init__(
        self,
        host: CollectionHost,
        units: SessionUnits,
        *,
        session_base: Path,
    ) -> None:
        self._host = host
        self._units = units
        self._base = Path(session_base)

    def __repr__(self) -> str:
        return "CodexLoginCollector()"

    async def collect(self, kind: ConnectionKind, holder: uuid.UUID) -> bool:
        try:
            return await self._collect(kind, holder)
        except Exception as error:
            logger.warning(
                "Codex login collection failed for now: kind=%s exception_type=%s",
                kind.value,
                log_type_name(error),
            )
            return False

    async def _collect(self, kind: ConnectionKind, holder: uuid.UUID) -> bool:
        lease = await self._host.session_lease(kind, holder)
        if lease is None:
            return True  # not this holder's any more: nothing to do here
        session = lease.session
        directory = self._base / str(session)
        # (1) The fence, before the unit is looked at.
        await asyncio.to_thread(fence, directory)
        # (2) Stopped?
        if not await self._units.stopped(session):
            with contextlib.suppress(Exception):
                await self._units.stop(session)
            if not await self._units.stopped(session):
                logger.error(
                    "A Codex session's unit is still running; its login is"
                    " collected once it stopped (systemctl stop"
                    " paw-cli-session@%s.service; do not delete /run/paw-cli/%s):"
                    " kind=%s",
                    session,
                    session,
                    kind.value,
                )
                return False
        if await self._units.egress_left(session):
            logger.error(
                "A Codex session's egress units are still active; stop them"
                " (systemctl stop paw-egress-session@%s.socket"
                " paw-egress-session@%s.service): kind=%s",
                session,
                session,
                kind.value,
            )
        # (3) The login.
        text, result = await asyncio.to_thread(read_session_login, directory)
        if text is not None:
            if not await self._host.renew_session(kind, holder):
                return True  # taken over meanwhile: not ours to write
            result = await self._host.write_collected_login(
                kind, lease.secret_handle, lease.expected_version, text
            )
            if result is None:
                logger.error(
                    "A Codex session's credential is older than the version it"
                    " was given; its slot stays (operator): kind=%s session=%s",
                    kind.value,
                    session,
                )
                return False
        assert result is not None
        # (4) Still stopped?
        if not await self._units.stopped(session):
            logger.error(
                "A Codex session's unit ran again after its login was read;"
                " its slot stays: kind=%s session=%s",
                kind.value,
                session,
            )
            return False
        # (5) The tombstone, then once more empty and stopped.
        await asyncio.to_thread(tombstone, directory)
        if not await asyncio.to_thread(empty, directory) or not (
            await self._units.stopped(session)
        ):
            logger.warning(
                "A Codex session's directory is not an empty tombstone yet:"
                " kind=%s session=%s",
                kind.value,
                session,
            )
            return False
        await self._host.finish_session(kind, holder, result)
        log = (
            logger.info
            if result
            in (
                SessionCollection.WRITTEN,
                SessionCollection.UNCHANGED,
                SessionCollection.DETACHED,
            )
            else logger.error
        )
        log(
            "A Codex session's login was collected: kind=%s session=%s result=%s",
            kind.value,
            session,
            result.value,
        )
        return True


def fence(directory: Path) -> None:
    """Decision 0096, 2's step 1. A directory that is gone (a reboot emptied
    ``/run``) is nothing to fence."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("not a session directory")
    (directory / LAUNCH_NAME).unlink(missing_ok=True)
    config = directory / CONFIG_DIR_NAME
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISDIR(os.lstat(config).st_mode):
            os.chmod(config, FENCE_MODE)
    os.chmod(directory, FENCE_MODE)


def read_session_login(
    directory: Path,
) -> tuple[str | None, SessionCollection | None]:
    """The session's ``auth.json``: its text, or the definite result
    (``missing``: no directory or no file; ``invalid``: not a JSON object or
    not a regular file). Another ``OSError`` passes (tried again)."""
    try:
        data = read_login_file(directory / CONFIG_DIR_NAME / AUTH_NAME)
    except FileNotFoundError:
        return None, SessionCollection.MISSING
    except ValueError:
        return None, SessionCollection.INVALID
    except OSError as error:
        if error.errno == errno.ELOOP:  # a link where the file should be
            return None, SessionCollection.INVALID
        raise
    try:
        text = data.decode("utf-8")
        value = json.loads(text)
    except ValueError:
        return None, SessionCollection.INVALID
    if not isinstance(value, dict):
        return None, SessionCollection.INVALID
    return text, None


def tombstone(directory: Path) -> None:
    """Decision 0096, 2's step 5: the directory's content goes, the
    directory stays empty and 0550."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("not a session directory")
    os.chmod(directory, _OPEN_MODE)
    try:
        for entry in os.scandir(directory):
            if entry.is_dir(follow_symlinks=False):
                os.chmod(entry.path, _OPEN_MODE)
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
    finally:
        os.chmod(directory, FENCE_MODE)


def empty(directory: Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True


class LoopHost(Protocol):
    async def session_leases(self) -> tuple: ...

    async def take_over_session(
        self, kind: ConnectionKind, previous: uuid.UUID, holder: uuid.UUID
    ) -> bool: ...

    async def renew_session(self, kind: ConnectionKind, holder: uuid.UUID) -> bool: ...


class CodexCollectionLoop:
    """Decision 0096, 1 D: in every backend process, every
    ``interval_seconds``, the slots bound to a session whose lease expired
    (their process is gone: a slot a living process keeps is renewed) are
    taken over (compare-and-swap on the holder: of several processes, one
    wins each) and kept until collected (``keep_until_collected``: renewed
    every ``heartbeat_seconds``, a round every ``interval_seconds``, the
    first at once). Unbound and unexpired rows are never touched."""

    def __init__(
        self,
        host: LoopHost,
        collector: SessionCollector,
        *,
        interval_seconds: float = COLLECT_INTERVAL_SECONDS,
        heartbeat_seconds: float = SLOT_HEARTBEAT_SECONDS,
    ) -> None:
        self._host = host
        self._collector = collector
        self._interval = float(interval_seconds)
        self._heartbeat = float(heartbeat_seconds)
        self._stop = asyncio.Event()
        self._kept: dict[uuid.UUID, asyncio.Task] = {}

    def __repr__(self) -> str:
        return "CodexCollectionLoop()"

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Until :meth:`stop`; the slots taken over are let go at the end
        (they expire and the next process takes them over)."""
        try:
            while not self._stop.is_set():
                try:
                    await self.cycle()
                except Exception as error:
                    logger.warning(
                        "Codex session collection loop failed for now:"
                        " exception_type=%s",
                        log_type_name(error),
                    )
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._interval):
                        await self._stop.wait()
        finally:
            for task in self._kept.values():
                task.cancel()
            for task in list(self._kept.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._kept.clear()

    async def cycle(self) -> int:
        """One look at the rows; how many slots were taken over."""
        for session, task in list(self._kept.items()):
            if task.done():
                del self._kept[session]
        taken = 0
        for lease in await self._host.session_leases():
            if not lease.expired or lease.session in self._kept:
                continue
            holder = uuid.uuid4()
            if not await self._host.take_over_session(lease.kind, lease.holder, holder):
                continue  # another process was first
            taken += 1
            logger.error(
                "A Codex session's slot outlived its process and was taken over"
                " for its collection: kind=%s session=%s",
                lease.kind.value,
                lease.session,
            )
            self._kept[lease.session] = asyncio.ensure_future(
                self._keep(lease.kind, holder)
            )
        return taken

    async def _keep(self, kind: ConnectionKind, holder: uuid.UUID) -> None:
        async def renew() -> bool:
            return await self._host.renew_session(kind, holder)

        async def collect() -> bool:
            return await self._collector.collect(kind, holder)

        await keep_until_collected(
            renew,
            collect,
            heartbeat_seconds=self._heartbeat,
            interval_seconds=self._interval,
            first_at_once=True,
        )


def build_codex_collection(
    service,
    *,
    session_base: Path,
    interval_seconds: float = COLLECT_INTERVAL_SECONDS,
    units: SessionUnits | None = None,
) -> CodexCollectionLoop:
    """Production's collection (Decision 0096, 1 D) on ``service`` (a
    ``ConnectionService`` with the secret store): the collector is attached
    to it (its own kept slots are collected by it), and the loop it returns
    takes over the slots of processes that are gone. ``units``: production's
    :class:`SystemdSessionUnits` on a ``SystemdLauncher`` by default."""
    units = units or SystemdSessionUnits(SystemdLauncher())
    collector = CodexLoginCollector(service, units, session_base=session_base)
    service.attach_session_collector(collector)
    return CodexCollectionLoop(service, collector, interval_seconds=interval_seconds)


__all__ = [
    "CGROUP_ROOT",
    "COLLECT_INTERVAL_SECONDS",
    "EGRESS_SOCKET_DIR",
    "FENCE_MODE",
    "CodexCollectionLoop",
    "CodexLoginCollector",
    "SessionUnits",
    "SystemdSessionUnits",
    "build_codex_collection",
    "empty",
    "fence",
    "read_session_login",
    "tombstone",
]
