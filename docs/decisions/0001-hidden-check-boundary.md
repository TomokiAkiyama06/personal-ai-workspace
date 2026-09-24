# Hidden check execution boundary

- Status: Proposed
- Date: 2026-09-21
- Scope: PAW-013 Test / Hidden Acceptance Runner

## Proposal

Benchmark task manifests retain only opaque hidden `reference_id` values.  The
evaluator resolves those identifiers from a private `HiddenCheckRegistry` only
after the candidate phase completes.  It runs the resulting commands with the
candidate worktree as their current directory, without copying the hidden source,
command, reference ID, or path into that worktree.

The candidate-facing interface receives visible checks only.  Durable evaluator
records use mode `0600` and retain each stream's bounded byte count, digest, and
truncation state rather than raw stdout or stderr.  Raw output is available only
to the trusted evaluator caller for immediate diagnosis.

The runner also applies safeguards that reduce accidents but are not the
boundary: an allowlisted check environment, output captured into bounded buffers
while the check runs, termination of the check's process group and known
descendants on timeout, an owner-only log created without a wider-permission window
and never opened through a symlink, and lookup errors that carry no chained
exception.

## Constraint

This Python runner separates data and process phases but is not a hostile-code
sandbox.  Production execution must enforce the boundary with separate OS
identities or an isolated container/mount namespace so code under test cannot
traverse to private evaluator storage.  The precise storage and container design
remain deferred benchmark details and require human/Admin approval.

## Residual risks of a hostile same-user candidate

Code under test runs as the evaluator's OS user.  In-process safeguards therefore
reduce accidents and detect some tampering; they do not stop a deliberate attacker.
What the attacker can still do, and why the Python runners cannot close it:

- **Read what the user can read.**  The evaluator's environment (`/proc/<pid>/environ`,
  narrowed by making the evaluator non-dumpable but not for ancestors or root), the real
  home directory, keyrings, agent sockets, world-readable `/proc/<pid>/cmdline`, and
  every other process of the same user.
- **Write what the user can write.**  Evaluator logs and state can be opened by path, so
  they are only checked for tampering (identity, size, links), not protected; the
  candidate's worktree shares the main repository's `.git`.
- **Outlive or evade process cleanup.**  A process that daemonizes (double fork plus
  `setsid`) leaves both the process group and the parent chain and cannot be found.
  Signalling by pid or group id is inherently check-then-act: a pid can be reused between
  the last identity check and the signal, and a process group cannot be signalled through
  a pidfd, so a window of microseconds remains.  Members forked into a group after its
  leader was reaped elsewhere, by members that all exit before the final signal, are missed.
- **Why not in-process.**  The kernel gives no privacy or containment between processes
  of one UID other than the coarse dumpable flag, and offers no way to enumerate or kill
  a tree of processes without an owner it controls: that is a PID namespace or a cgroup.
  Adding more heuristics only shrinks windows.

Production execution must use what closes these: a separate OS identity for candidate
and check code, a container or PID and mount namespace with a cgroup v2 hierarchy (killed
as a unit with `cgroup.kill`), evaluator storage and secrets outside that namespace (or
mounted read-only), and no evaluator credentials in the environment of any ancestor.
