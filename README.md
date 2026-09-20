# Personal AI Workspace

Self-hosted, local-first AI workspace for coding agents, long-term memory, research, and GPU orchestration.

> **Status:** Requirements Freeze Candidate / early development.\
> The first implementation milestone is a reproducible **Benchmark / Evaluator Harness** used to select local coding and memory models.

## Goals

Personal AI Workspace is designed as a self-hosted control plane that combines:

- local LLMs and coding agents
- Codex and Claude Code as shared cloud agents
- long-term structured memory
- Git / GitHub task workflows
- parallel agent orchestration with isolated worktrees
- web research with evidence tracking
- GPU / VRAM resource scheduling
- recovery-oriented, machine-readable workspace projections

The project follows these principles:

- **Local-first, model-agnostic**
- **Evidence over confidence**
- **Human-controlled high-risk actions and merge authority**
- **Memory as structured data**
- **Parallel-first where dependencies allow**
- **Secrets isolated from agents**
- **Self-hosted and privacy-conscious by design**

## Current development model

During the initial implementation phase:

- **Codex** is the primary implementation agent.
- **Claude Code** is used as an independent reviewer for architecture, security, maintainability, and requirement alignment.
- The **Evaluator** is the primary machine-verifiable success layer.
- Human approval remains the final authority for sensitive actions and merge, unless a task explicitly grants merge authority.

After the benchmark phase, suitable local coding models may become primary or parallel workers.

## First milestone

The first development milestone is the **Benchmark / Evaluator Harness**.

It will compare candidate models under the same:

- repository and starting commit
- issue/task specification
- system prompt and tool schema
- runtime/resource limits
- tests and hidden acceptance criteria

Key measurements include correctness, regression rate, human correction time, tool failures, wall-clock time, and VRAM usage.

See [`docs/BENCHMARK_EVALUATOR.md`](docs/BENCHMARK_EVALUATOR.md).

## Documentation

The authoritative project requirements are in [`REQUIREMENTS.md`](REQUIREMENTS.md).

Key design documents:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/MEMORY_ARCHITECTURE.md`](docs/MEMORY_ARCHITECTURE.md)
- [`docs/SECURITY_RBAC_AUDIT.md`](docs/SECURITY_RBAC_AUDIT.md)
- [`docs/SECURITY_TOOL_PERMISSIONS.md`](docs/SECURITY_TOOL_PERMISSIONS.md)
- [`docs/NOTIFICATION_POLICY.md`](docs/NOTIFICATION_POLICY.md)
- [`docs/UI_DESIGN.md`](docs/UI_DESIGN.md)
- [`docs/MODEL_CANDIDATES.md`](docs/MODEL_CANDIDATES.md)
- [`docs/BENCHMARK_EVALUATOR.md`](docs/BENCHMARK_EVALUATOR.md)
- [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md)
- [`docs/DEPLOYMENT_UPDATE.md`](docs/DEPLOYMENT_UPDATE.md)
- [`docs/REQUIREMENTS_FREEZE_REVIEW.md`](docs/REQUIREMENTS_FREEZE_REVIEW.md)

Agent rules are defined in [`AGENTS.md`](AGENTS.md).

> Most detailed design documents are currently written in Japanese.

## Repository and recovery boundary

This public repository contains the application source and public design documents.

It **must not** contain real workspace recovery data, user memory, raw conversations, credentials, API tokens, private keys, or production secrets. A real Recovery Repository is a separate **private** repository.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).

## Security

Please read [`SECURITY.md`](SECURITY.md) before reporting a vulnerability. Never post credentials or private workspace data in a public issue.
