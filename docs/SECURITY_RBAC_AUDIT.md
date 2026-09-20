# Security / RBAC / Audit — Personal AI Workspace

更新日: 2026-09-20

## 1. Roles
### User
Chat、Agent利用、自分のWorkspace/GitHub/Memory、PR作成。

### Admin
User権限 + Usage / Audit / System Prompt / Model / Routing / Permission / Config管理。

### Owner
Adminの全権限を含むシステムの最終所有者。原則1名。
Adminの追加/削除、Owner権限の移譲、最後のAdmin削除防止に関わる操作、全体復旧/非常時設定はOwnerのみ。
削除待ちUserの30日以内の復元と、Backup機能の有効/無効、Backup先、Backup専用Credential、Backup方式・Backup関連の重要Security設定の変更もOwnerのみ。
詳細は [REQUIREMENTS.md](../REQUIREMENTS.md) の「Owner / Admin の役割分離」「User Deletion Retention」「Backup Authority」に従う。

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
Admin向けAPI内でも、Adminの追加/削除やOwner権限移譲等のOwner専用操作は、操作単位でOwnerを要求する。

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
