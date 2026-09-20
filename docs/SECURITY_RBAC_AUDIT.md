# Security / RBAC / Audit — Personal AI Workspace

更新日: 2026-09-15

## 1. Roles
### User
Chat、Agent利用、自分のWorkspace/GitHub/Memory、PR作成。

### Admin
User権限 + Usage / Audit / System Prompt / Model / Routing / Permission / Config管理。

### System
Backend内部identity。人間ログイン不可。

## 2. Admin-only
```text
/admin/users
/admin/usage
/admin/audit
/admin/system-prompts
/admin/models
/admin/routing
/admin/permissions
/admin/config
```
Frontend表示だけでなくBackendでRole check。

## 3. Merge authority
原則Human-only。
Agent MergeはHumanが現在Taskで明示委任した場合のみ。

## 4. System Prompt
- Admin only
- versioned
- diff retained
- rollback
- audit
User promptからGlobal Policyを上書き不可。

## 5. Usage Dashboard
User別:
- Local/Codex/Claude tasks
- runtime
- GPU time
- token/context（取得可能範囲）
- Agent steps
- Tools
- Repos/PRs
- changed files
- tests
- errors/escalation

## 6. Audit
Append-onlyを基本。
`timestamp, actor, role, action, target, project, repo, agent, request_id, result, metadata`

## 7. Credential
- plaintext DB禁止
- user単位
- least privilege
- log出力禁止
- Secretを不要にLLM Contextへ入れない

Credential vaultの具体方式は[IMPLEMENTATION_CHOICE]。Security要件はFIXED。



## External Agent authentication

[FIXED]

GitHub:
- Userごとの `gh auth login`
- OS Userごとにcredentialを分離

Codex / Claude:
- Workspace全体で共有するSystem-level Connection
- Owner / Adminが接続・更新・削除
- SecretはBackend Secret Store / Tool Broker内部のみで利用
- General User / Agentへplaintextを見せない

Shared Connectionの利用量はUser / Task単位でattributionし、
Per-user quota / concurrency / usage policyを適用する。

## Shared Memory administration

[FIXED]

Shared Memoryのcreate / edit / delete / restore / promotion approvalはOwner/Adminのみ。
Active Userはread可能。
AgentによるWorkspace-wide auto-promotionはBackend Policyで拒否する。
