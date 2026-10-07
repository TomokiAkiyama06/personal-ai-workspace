"""Stopwatch for the human correction time of a benchmark candidate (Decision 0041 8, 0082).

A person who corrects a candidate's unresolved result records explicit events in an
append-only JSON Lines log.  The correction time is the active time between ``start``
and ``finish`` minus the paused intervals, capped at 60 minutes (Decision 0041 8: a
correction that does not end within the cap is recorded as unresolved).

Each line of the log is one event::

    {"v": 1, "event": "start", "session": "M/T/run1", "at": "2026-10-08T01:02:03.456Z",
     "task_id": "T", "model": "M", "run": "run1"}
    {"v": 1, "event": "pause", "session": "M/T/run1", "at": "..."}

Events are ``start``, ``pause``, ``resume``, ``finish`` (the evaluator re-run passed
and the person accepted the result), ``abandon`` (the person gave up) and ``unchanged``
(the candidate's result was accepted without a change: 0 ms).  The log holds task ids,
model names and timestamps only; it never holds code, test output or notes, because
the hidden checks of a dataset must not leak through it (Decision 0041 4).

Commands::

    python -m benchmarks.human_correction --log FILE start --task T --model M [--run R]
    python -m benchmarks.human_correction --log FILE pause|resume|finish|abandon [--session S]
    python -m benchmarks.human_correction --log FILE unchanged --task T --model M [--run R]
    python -m benchmarks.human_correction --log FILE status [--session S]
    python -m benchmarks.human_correction --log FILE summary [--output FILE]
    python -m benchmarks.human_correction --log FILE apply --session S --result FILE

``--session`` may be omitted when exactly one session is open.  ``apply`` writes the
session's ``human_correction_ms`` into ``metrics`` of an evaluator Result (schema v1).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.json_input import decode_json
from benchmarks.validate_result import validate_document

LOG_VERSION = 1
CAP_MS = 60 * 60 * 1000
EVENTS = ("start", "pause", "resume", "finish", "abandon", "unchanged")
OPEN_STATES = ("running", "paused")
# Outcome -> whether the person ended with an accepted result.
OUTCOMES = {
    "accepted": True,
    "unchanged": True,
    "capped": False,
    "abandoned": False,
}


class CorrectionLogError(ValueError):
    """The log or the requested transition is invalid (exit code 1)."""


@dataclass
class Session:
    session: str
    task_id: str
    model: str
    run: str
    started: datetime
    state: str = "running"
    active_ms: float = 0.0
    pauses: int = 0
    ended: datetime | None = None
    end_event: str | None = None
    _since: datetime | None = field(default=None, repr=False)

    def elapsed_ms(self, now: datetime) -> float:
        """Active time so far (running time up to ``now`` included)."""
        if self.state == "running" and self._since is not None:
            return self.active_ms + _ms(self._since, now)
        return self.active_ms

    @property
    def outcome(self) -> str | None:
        if self.end_event is None:
            return None
        if self.end_event == "unchanged":
            return "unchanged"
        if self.active_ms > CAP_MS:
            return "capped"
        return "accepted" if self.end_event == "finish" else "abandoned"

    @property
    def human_correction_ms(self) -> int | None:
        """The Result metric: omitted (None) while open, the cap when unresolved."""
        outcome = self.outcome
        if outcome is None:
            return None
        if outcome == "unchanged":
            return 0
        if outcome == "accepted":
            return round(self.active_ms)
        return CAP_MS

    def report(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "session": self.session,
            "task_id": self.task_id,
            "model": self.model,
            "run": self.run,
            "state": self.state,
            "started": _format(self.started),
            "active_ms": round(self.active_ms),
            "pauses": self.pauses,
        }
        if self.ended is not None:
            document["ended"] = _format(self.ended)
            document["wall_ms"] = round(_ms(self.started, self.ended))
            document["outcome"] = self.outcome
            document["human_resolved"] = OUTCOMES[self.outcome]
            document["human_correction_ms"] = self.human_correction_ms
        return document


def session_id(model: str, task_id: str, run: str) -> str:
    return f"{model}/{task_id}/{run}"


def _ms(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


def _format(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _parse_time(value: Any, line: int) -> datetime:
    if not isinstance(value, str):
        raise CorrectionLogError(f"line {line}: 'at' must be a string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CorrectionLogError(f"line {line}: 'at' is not an ISO 8601 time") from None
    if moment.tzinfo is None:
        raise CorrectionLogError(f"line {line}: 'at' has no time zone")
    return moment


def _name(value: Any, key: str, line: int) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value:
        raise CorrectionLogError(
            f"line {line}: '{key}' must be a non-empty string without '/'"
        )
    return value


def replay(lines: Iterable[str]) -> dict[str, Session]:
    """Rebuild every session from the log, rejecting impossible transitions."""
    sessions: dict[str, Session] = {}
    previous: datetime | None = None
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            event = decode_json(text)
        except ValueError:
            raise CorrectionLogError(f"line {number}: not valid JSON") from None
        if not isinstance(event, dict) or event.get("v") != LOG_VERSION:
            raise CorrectionLogError(f"line {number}: not a version 1 event")
        kind = event.get("event")
        if kind not in EVENTS:
            raise CorrectionLogError(f"line {number}: unknown event")
        at = _parse_time(event.get("at"), number)
        if previous is not None and at < previous:
            raise CorrectionLogError(f"line {number}: time goes backwards")
        previous = at
        name = event.get("session")
        if kind in ("start", "unchanged"):
            task_id = _name(event.get("task_id"), "task_id", number)
            model = _name(event.get("model"), "model", number)
            run = _name(event.get("run"), "run", number)
            if name != session_id(model, task_id, run):
                raise CorrectionLogError(f"line {number}: session id does not match")
            if name in sessions:
                raise CorrectionLogError(f"line {number}: session already recorded")
            session = Session(name, task_id, model, run, started=at, _since=at)
            if kind == "unchanged":
                session.state, session.ended, session.end_event = "closed", at, kind
            sessions[name] = session
            continue
        session = sessions.get(name) if isinstance(name, str) else None
        if session is None:
            raise CorrectionLogError(f"line {number}: unknown session")
        _apply(session, kind, at, number)
    return sessions


def _apply(session: Session, kind: str, at: datetime, line: int) -> None:
    if session.state not in OPEN_STATES:
        raise CorrectionLogError(f"line {line}: session is already closed")
    if kind == "pause":
        if session.state != "running":
            raise CorrectionLogError(f"line {line}: session is not running")
        session.active_ms += _ms(session._since, at)
        session.state, session._since = "paused", None
        session.pauses += 1
    elif kind == "resume":
        if session.state != "paused":
            raise CorrectionLogError(f"line {line}: session is not paused")
        session.state, session._since = "running", at
    else:
        # finish / abandon while paused end at the pause: the break is not counted.
        session.active_ms = session.elapsed_ms(at)
        session.state, session._since = "closed", None
        session.ended, session.end_event = at, kind


class CorrectionLog:
    def __init__(
        self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ):
        self.path = path
        self.clock = clock

    def sessions(self) -> dict[str, Session]:
        if not os.path.lexists(self.path):
            return {}
        descriptor = self._open_checked(os.O_RDONLY, fcntl.LOCK_SH)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return replay(handle.read().splitlines())

    def _open_locked(self) -> int:
        """Open the log for appending, make it private and hold an exclusive lock
        until the descriptor is closed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
        return self._open_checked(flags, fcntl.LOCK_EX, private=True)

    def _open_checked(self, flags: int, lock: int, private: bool = False) -> int:
        """Open without following a link and refuse anything but the caller's own
        regular file (reads and writes alike)."""
        try:
            descriptor = os.open(self.path, flags | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise CorrectionLogError(
                "log must be a regular file (not a link)"
            ) from None
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise CorrectionLogError("log must be a regular file (not a link)")
            if status.st_uid != os.geteuid():
                raise CorrectionLogError("log must be owned by the current user")
            if private:
                os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, lock)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def record(self, kind: str, name: str, **fields: str) -> Session:
        """Validate the event against the current log, append it and return the session.

        The read, the validation and the append run under one exclusive lock, so two
        commands on the same log cannot both validate against the same snapshot."""
        descriptor = self._open_locked()
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            handle.seek(0)
            content = handle.read()
            lines = content.splitlines()
            event = {"v": LOG_VERSION, "event": kind, "session": name}
            event["at"] = _format(self.clock())
            event.update(fields)
            # Replaying the log plus the new line applies exactly the rules a reader
            # applies, so an event the reader would reject is never written.
            sessions = replay([*lines, json.dumps(event)])
            # A last line without its newline (an edited log) must stay its own line.
            separator = "\n" if content and not content.endswith("\n") else ""
            handle.write(separator + json.dumps(event, ensure_ascii=False) + "\n")
        return sessions[name]

    def resolve(self, name: str | None) -> str:
        if name is not None:
            return name
        open_sessions = [
            s.session for s in self.sessions().values() if s.state in OPEN_STATES
        ]
        if len(open_sessions) != 1:
            raise CorrectionLogError(
                f"{len(open_sessions)} sessions are open; pass --session"
            )
        return open_sessions[0]


def apply_to_result(session: Session, result_path: Path) -> dict[str, Any]:
    """Write the session's metric into an evaluator Result and validate it."""
    value = session.human_correction_ms
    if value is None:
        raise CorrectionLogError("session is still open")
    try:
        document = decode_json(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise CorrectionLogError("cannot read the result as JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("metrics"), dict):
        raise CorrectionLogError("result has no metrics object")
    if document.get("task_id") != session.task_id:
        raise CorrectionLogError("result task_id does not match the session")
    candidate = document.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("model") != session.model:
        raise CorrectionLogError("result candidate model does not match the session")
    document["metrics"]["human_correction_ms"] = value
    errors = validate_document(document)
    if errors:
        raise CorrectionLogError("result is invalid: " + "; ".join(errors))
    handle, temporary = tempfile.mkstemp(dir=result_path.parent, suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, result_path)
    return document


def write_summary(output: Path, log: Path, text: str) -> None:
    """Write the summary without ever touching the event log (also via a link)."""
    try:
        same = output.exists() and log.exists() and os.path.samefile(output, log)
    except OSError:
        same = True
    if same or output.resolve() == log.resolve():
        raise CorrectionLogError("--output must not be the event log")
    # A new file replaces the directory entry: a link at the output path is replaced,
    # not written through.
    handle, temporary = tempfile.mkstemp(dir=output.absolute().parent, suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, output)


def summary(sessions: dict[str, Session], now: datetime) -> dict[str, Any]:
    reports = []
    for session in sessions.values():
        report = session.report()
        if session.state in OPEN_STATES:
            report["active_ms"] = round(session.elapsed_ms(now))
        reports.append(report)
    closed = [s for s in sessions.values() if s.outcome is not None]
    counts = {outcome: 0 for outcome in OUTCOMES}
    for session in closed:
        counts[session.outcome] += 1
    return {
        "cap_ms": CAP_MS,
        "sessions": reports,
        "outcomes": counts,
        "open": len(sessions) - len(closed),
    }


def main(
    argv: list[str] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(description="Human correction time stopwatch.")
    parser.add_argument("--log", type=Path, required=True, help="event log (JSONL)")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "unchanged"):
        start = sub.add_parser(command)
        start.add_argument("--task", required=True)
        start.add_argument("--model", required=True)
        start.add_argument("--run", default="run1")
    for command in ("pause", "resume", "finish", "abandon", "status"):
        sub.add_parser(command).add_argument("--session")
    sub.add_parser("summary").add_argument("--output", type=Path)
    apply = sub.add_parser("apply")
    apply.add_argument("--session", required=True)
    apply.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args(argv)

    log = CorrectionLog(arguments.log, clock)
    try:
        if arguments.command in ("start", "unchanged"):
            name = session_id(arguments.model, arguments.task, arguments.run)
            session = log.record(
                arguments.command,
                name,
                task_id=arguments.task,
                model=arguments.model,
                run=arguments.run,
            )
            print(json.dumps(session.report(), ensure_ascii=False))
            return 0
        if arguments.command in ("pause", "resume", "finish", "abandon"):
            session = log.record(arguments.command, log.resolve(arguments.session))
            print(json.dumps(session.report(), ensure_ascii=False))
            return 0
        if arguments.command == "status":
            name = log.resolve(arguments.session)
            session = log.sessions().get(name)
            if session is None:
                raise CorrectionLogError("unknown session")
            now = clock()
            report = session.report()
            report["active_ms"] = round(session.elapsed_ms(now))
            if session.state in OPEN_STATES and report["active_ms"] > CAP_MS:
                report["cap_reached"] = True
            print(json.dumps(report, ensure_ascii=False))
            return 0
        if arguments.command == "summary":
            text = json.dumps(
                summary(log.sessions(), clock()), indent=2, ensure_ascii=False
            )
            if arguments.output:
                write_summary(arguments.output, arguments.log, text + "\n")
            else:
                print(text)
            return 0
        session = log.sessions().get(arguments.session)
        if session is None:
            raise CorrectionLogError("unknown session")
        apply_to_result(session, arguments.result)
        print(f"{arguments.result}: human_correction_ms={session.human_correction_ms}")
        return 0
    except CorrectionLogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
