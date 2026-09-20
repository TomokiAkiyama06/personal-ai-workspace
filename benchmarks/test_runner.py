"""Run visible and evaluator-owned hidden benchmark checks.

Hidden check definitions never enter a candidate worktree or the durable run log.
The registry is constructed by the evaluator from private configuration after the
candidate has finished.  It deliberately maps opaque task-manifest reference IDs
to commands without exposing that mapping through this module's public results.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_MAX_CAPTURE_BYTES = 64 * 1024


@dataclass(frozen=True)
class CheckDefinition:
    """An evaluator check command; hidden definitions stay outside task manifests."""

    id: str
    type: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class CheckExecution:
    """One check's in-memory output and safe, durable execution metadata."""

    id: str
    type: str
    visibility: str
    status: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


class HiddenCheckRegistry:
    """Evaluator-owned mapping from opaque reference IDs to hidden commands."""

    def __init__(self, checks: Mapping[str, CheckDefinition]):
        if not checks:
            self._checks: dict[str, CheckDefinition] = {}
            return
        self._checks = {}
        for reference_id, check in checks.items():
            if not isinstance(reference_id, str) or not reference_id:
                raise ValueError("hidden check reference IDs must be non-empty strings")
            _validate_check(check)
            if check.id in {item.id for item in self._checks.values()}:
                raise ValueError("hidden check IDs must be unique")
            self._checks[reference_id] = check

    def resolve(self, reference_id: str) -> CheckDefinition:
        """Resolve one private reference without including it in any error output."""
        try:
            return self._checks[reference_id]
        except KeyError as error:
            raise KeyError("hidden check reference is unavailable") from error


class TestRunner:
    """Execute checks in an existing candidate worktree and retain safe evidence.

    ``execution_log`` must be evaluator-owned and outside ``worktree``.  Raw
    stdout/stderr are returned to the trusted caller for immediate diagnosis, but
    durable records retain only byte counts, SHA-256 digests, and truncation flags.
    This avoids creating a credential store while still preserving evidence that
    output was captured.
    """

    def __init__(self, worktree: Path, execution_log: Path):
        self.worktree = Path(worktree).resolve()
        self.execution_log = Path(execution_log).resolve()
        if not self.worktree.is_dir():
            raise ValueError("worktree must be an existing directory")
        if self.execution_log.is_relative_to(self.worktree):
            raise ValueError("execution_log must be outside the candidate worktree")
        self.execution_log.parent.mkdir(parents=True, exist_ok=True)

    def run_visible(
        self, checks: Sequence[CheckDefinition], timeout_seconds: float
    ) -> tuple[CheckExecution, ...]:
        """Run task-manifest checks that may be disclosed to the candidate."""
        return self._run(checks, "visible", timeout_seconds)

    def run_hidden(
        self,
        reference_ids: Sequence[str],
        registry: HiddenCheckRegistry,
        timeout_seconds: float,
    ) -> tuple[CheckExecution, ...]:
        """Resolve and run private checks after candidate execution has ended."""
        checks = tuple(registry.resolve(reference_id) for reference_id in reference_ids)
        return self._run(checks, "hidden", timeout_seconds)

    def _run(
        self,
        checks: Sequence[CheckDefinition],
        visibility: str,
        timeout_seconds: float,
    ) -> tuple[CheckExecution, ...]:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        results = []
        for check in checks:
            _validate_check(check)
            result = self._execute(check, visibility, timeout_seconds)
            self._write_record(result)
            results.append(result)
        return tuple(results)

    def _execute(
        self, check: CheckDefinition, visibility: str, timeout_seconds: float
    ) -> CheckExecution:
        started = time.monotonic()
        status = "error"
        exit_code: int | None = None
        timed_out = False
        stdout = b""
        stderr = b""
        try:
            process = subprocess.Popen(
                check.command,
                cwd=self.worktree,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate(process)
                stdout, stderr = process.communicate()
            exit_code = process.returncode
            status = (
                "timed_out" if timed_out else ("passed" if exit_code == 0 else "failed")
            )
        except OSError:
            status = "error"
        stdout_bytes = len(stdout)
        stderr_bytes = len(stderr)
        stdout, stdout_truncated = _bounded(stdout)
        stderr, stderr_truncated = _bounded(stderr)
        return CheckExecution(
            id=check.id,
            type=check.type,
            visibility=visibility,
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _write_record(self, result: CheckExecution) -> None:
        record = {
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "id": result.id,
            "status": result.status,
            "stderr": _safe_stream_record(
                result.stderr, result.stderr_bytes, result.stderr_truncated
            ),
            "stdout": _safe_stream_record(
                result.stdout, result.stdout_bytes, result.stdout_truncated
            ),
            "timed_out": result.timed_out,
            "type": result.type,
            "visibility": result.visibility,
        }
        with self.execution_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
        os.chmod(self.execution_log, 0o600)


def _validate_check(check: CheckDefinition) -> None:
    if not check.id or not check.type:
        raise ValueError("check ID and type must be non-empty")
    if not check.command or not all(
        isinstance(item, str) and item for item in check.command
    ):
        raise ValueError("check command must contain non-empty strings")


def _bounded(stream: bytes) -> tuple[bytes, bool]:
    return stream[:_MAX_CAPTURE_BYTES], len(stream) > _MAX_CAPTURE_BYTES


def _safe_stream_record(
    stream: bytes, total_bytes: int, truncated: bool
) -> dict[str, object]:
    return {
        "captured_bytes": total_bytes,
        "sha256": hashlib.sha256(stream).hexdigest(),
        "truncated": truncated,
    }


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
