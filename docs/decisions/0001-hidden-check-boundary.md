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

## Constraint

This Python runner separates data and process phases but is not a hostile-code
sandbox.  Production execution must enforce the boundary with separate OS
identities or an isolated container/mount namespace so code under test cannot
traverse to private evaluator storage.  The precise storage and container design
remain deferred benchmark details and require human/Admin approval.
