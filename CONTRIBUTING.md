# Contributing

Contributions are welcome. This project is still in early development, so large implementation changes should stay aligned with `REQUIREMENTS.md`.

## Before changing code

1. Read `REQUIREMENTS.md`.
2. Read `AGENTS.md`.
3. Check related design documents under `docs/`.
4. Prefer an Issue for behavior or architecture changes that are not already specified.

## Pull requests

PRs should include:

- what changed
- why it changed
- related Issue / Task
- tests or verification performed
- known limitations
- security / migration impact, when relevant
- whether AI agents were used

Japanese or English is acceptable. The maintainer may use Japanese for review and project-management discussion.

## AI-assisted contributions

AI-assisted implementation is allowed and expected.

For this repository's initial development:

- Codex is the primary implementation agent.
- Claude Code is commonly used for independent review.
- AI-generated code is not considered correct merely because the agent reports success.
- Tests, acceptance criteria, evaluator results, and human review take priority.

## Git workflow

- Do not push directly to `main`.
- Work on a feature / task branch.
- Open a Pull Request.
- Do not force-push protected/shared branches without explicit authorization.
- Merge authority follows `AGENTS.md`.

## Security

Never commit real secrets, credentials, private recovery data, real user memory, or raw private conversations.

See `SECURITY.md`.
