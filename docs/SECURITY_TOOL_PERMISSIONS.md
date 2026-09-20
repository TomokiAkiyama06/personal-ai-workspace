# Security / Tool Permissions

更新日: 2026-09-20
Status: [FIXED DIRECTION / APPROVAL BOUNDARY OPEN]

## Core model

- Tool Broker
- Capability Policy
- Secret Isolation
- Human Approval for dangerous operations

## Capabilities

- read
- write
- execute
- network
- credential-use
- destructive

## Secret isolation

AgentへAPI key / token / credential plaintextを原則渡さない。
Backend Tool BrokerがPolicy確認後にcredentialを付与してTool / External Serviceを実行する。

## Enforcement

Permission / ACL / quota / Task Budget / approval stateはBackendが最終判定する。
LLMは自身のpromptやTool requestによって権限を拡張できない。

## Approval

通常の低リスク操作はPolicy内で自動許可する。
destructive / privileged / permission-changing / credential-sensitive等の危険操作はHuman Approval対象とする。

具体的なApproval matrixは次の要件定義項目で確定する。



## Approval levels

| Level | Meaning | Typical examples |
|---|---|---|
| AUTO | read-only / low-risk execution | read, search, build, test, lint, logs |
| SCOPED_AUTO | write allowed only inside authorized Task scope | code edit, test edit, worktree, commit, AI branch push, PR create |
| APPROVAL | explicit human confirmation | destructive migration, broad delete, host/service change, large external impact |
| STRONG_APPROVAL | explicit approval + step-up where policy requires | merge, force push, protected branch direct push, ACL/role, credentials, sudo, critical delete |
| DENY | not exposed to Agent even with normal approval | credential plaintext, private key read, ACL bypass, self privilege escalation |

## Git boundary

Normal autonomous coding flow may proceed through an AI-owned branch and PR creation.
Merge remains human-controlled unless explicit merge authority has been granted for the task/session.

## Project vs host environment

Project-local dependency changes can be SCOPED_AUTO.
Host-wide OS/package/service/firewall/mount changes require APPROVAL or stronger.

## External write

Task-scoped explicit user authorization can grant narrowly scoped external-write capability.
The Agent cannot generalize one authorization into unrelated external writes.
