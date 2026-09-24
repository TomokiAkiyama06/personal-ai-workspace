# Architecture — Personal AI Workspace

更新日: 2026-09-15

## 1. Target
Ubuntu GPU Serverを計算・状態管理の中心に置き、Mac / Windows / Smartphone / IDEから同じWorkspaceへアクセスする。

## 2. Logical Components
```text
Clients
├─ Desktop App
├─ Web/PWA
└─ IDE (Remote SSH + future extension)
        │
Gateway
├─ HTTPS
├─ WebSocket
└─ Auth
        │
Core Backend
├─ RBAC
├─ Session Manager
├─ Project Manager
├─ Memory Manager
├─ Agent Orchestrator
├─ Evaluator
├─ Git Controller
├─ Audit Service
├─ Admin Service
└─ GPU Manager
        │
        ├─ Local Model Service
        ├─ Codex Adapter
        ├─ Claude Adapter
        └─ Tool Runtime
```

## 3. Execution Boundary
BackendはUbuntu上で動くため、同一Ubuntu内Repo操作にSSHは不要。
SSHはIDE・管理・将来Remote Execution Host用。

## 4. API [PROVISIONAL]
- REST: CRUD / Project / Config / Admin
- WebSocket: Chat / Agent / Tool / Test stream
- OpenAI-compatible internal API: Local model
- IDE Context API: future extension

## 5. Process separation
CoreはGPU非依存。
GPU AI serviceだけ停止可能。

## 6. Multi-user isolation
- App RBAC
- Linux user separation
- HOME separation
- Repo permission
- GitHub CLI separation
- Memory scope
- Session scope

## 7. Agent Adapter
共通interfaceへ寄せる。

This production Agent Adapter is distinct from the benchmark-only
[`CandidateAdapter`](../benchmarks/candidate_adapter.py), which defines one
candidate attempt and its benchmark controls.

```python
Agent.run(task, workspace, context, permissions, session) -> AgentResult
```

特定モデル/ベンダー仕様をCoreへ漏らしすぎない。



## Current architecture additions

- Backend Orchestrator owns Task DAG, dependency resolution, dynamic parallelism, permissions, budgets, worktrees, retry/escalation and integration.
- Write-capable parallel Workers use isolated branches/worktrees and converge through a Task integration worktree.
- Shared Main LLM Runtime serves multiple Local Agents; Agent count does not imply one model copy per Agent.
- Compute Resource Scheduler manages Local GPU/CPU and Cloud Agent capacity dynamically.
- Research is abstracted behind Research Provider adapters and uses a temporary Research Scratch store.
- Tool access passes through Backend Tool Broker with Capability Policy and Secret Isolation.



## Storage placement

- NVMe: PostgreSQL, pgvector, repos/worktrees, runtime and latency-sensitive data.
- HDD: primary local model store, Memory Markdown Projection, DB/WAL backups, archive data.
- Optional NVMe Hot Model Cache can stage frequently used models from HDD.
- Model cache is disposable and is not a source of truth.



## Model storage operating mode

Model storage differs by phase.

```text
Benchmark / model selection:
NVMe SSD -> GPU

Production:
8TB HDD -> GPU
```

Benchmark時は候補ModelをNVMeへstageし、頻繁なmodel switchingとcold-load overheadを抑える。

本番運用ではHDDをcanonical model storeとし、
Main Modelを一度GPUへloadした後は長時間residentさせる。

OptionalなNVMe Hot Model Cacheは一時的な高速化用途であり、
canonical copyではない。



## Backup boundary

- The dedicated Recovery Repository is the external fallback for Memory Markdown Projection and other defined recovery state.
- PostgreSQL dumps/WAL are not committed to Git.
- Local DB recovery may use the 8TB HDD.
- Full off-server DB backup is a future extension, not a V1 requirement.



## Disaster recovery repository

A dedicated Private Git repository stores a machine-readable Recovery Projection.

Normal operation:
- PostgreSQL is operational truth.
- Recovery Git is disaster-recovery state only.

Restore path:
Fresh Ubuntu -> Workspace install -> Recovery repo clone -> schema/version migration ->
PostgreSQL rebuild -> Project/Repo/User/Memory/ACL/Policy restore -> Repo re-clone ->
credentials re-register.

Secrets, database binaries, WAL, password/passkey material and private keys are excluded.



## Shared cloud-agent connections

GitHub identity is per Linux User.

Codex and Claude use shared Workspace-level connections:

```text
User Task
  ↓
Backend Orchestrator
  ↓
Quota / Policy check
  ↓
Codex or Claude Adapter
  ↓
Shared System Credential via Tool Broker
```

Usage is attributed back to the initiating User / Task even though the credential is shared.



## IDE / remote development

V1 does not require a custom IDE extension.

Primary developer access:
- Existing Remote SSH GUI such as VS Code Remote SSH
- SSH / CLI
- Personal AI Workspace GUI

The Workspace backend remains IDE-agnostic.

CLI and GUI use the same Backend API, permission checks, Task state, Tool Broker and Agent Orchestrator.

Unsaved editor buffers are outside the V1 backend context unless the user explicitly provides them.
Agent work remains isolated in dedicated branches/worktrees.
