# Security Policy

## Reporting a vulnerability

Do **not** report security vulnerabilities, credentials, tokens, private memory, or recovery data in a public GitHub issue.

Please use GitHub's private vulnerability reporting / Security Advisory flow for this repository when available.

Include:

- affected version / commit
- impact
- reproduction steps
- relevant logs with secrets removed
- suggested mitigation, if known

## Secret handling

This project is designed around **Tool Broker + Capability Policy + Secret Isolation**.

Agents and normal users should not receive plaintext credentials unless explicitly required by an implementation that has been reviewed for that purpose.

The public repository must never contain:

- API tokens
- GitHub / Codex / Claude credentials
- SSH private keys
- passkey secret material
- encryption / recovery keys
- real user memory or raw conversations
- real Recovery Repository content

## Supported versions

The project is currently in early development and does not yet have a stable release line. Security support policy will be updated before the first stable release.
