# Observability / System Health

更新日: 2026-09-20\
Status: [FIXED BASELINE / RETENTION OPEN]

## Principles

- Normal state should stay quiet.
- Abnormal state should become progressively more visible.
- Metrics are operational data, not user private-content inspection.
- Notification severity follows `NOTIFICATION_POLICY.md`.

## Core signals

### GPU / Model
- GPU utilization
- VRAM used / reserved / available
- Main Model residency / health
- Memory Worker status
- Local inference concurrency
- OOM / model load failure / unload failure

### Task / Agent
- Queued / Running / Waiting / Failed counts
- Task latency
- Retry / loop-detection count
- Tool failure
- Escalation
- Local / Codex / Claude status

### Database / Recovery
- PostgreSQL health
- connection / migration failure
- Recovery Projection generation
- last commit / push
- push failure / retry
- recovery-format validation

### External dependencies
- GitHub
- Codex
- Claude
- configured Web/Research provider

## UI

Normal:
- compact health indicator
- no intrusive banner

Abnormal:
- Notification Center
- warning/error badge
- non-modal banner for sustained ERROR
- modal/dialog only for CRITICAL situations that require immediate user action

## Open: metrics retention

To decide next:
- which metrics are persisted
- raw sampling interval
- retention duration
- aggregation / downsampling
- log retention
- per-user visibility boundary



## Metrics retention

[FIXED]

| Period | Resolution |
|---|---|
| Last 24 hours | 10-30 seconds |
| Last 7 days | 1 minute |
| Last 30 days | 5 minutes |
| Older | 1 hour aggregate |

Event logs are stored separately from numerical metrics.

Retention guidance:
- Task detail / raw tool output: 90 days
- Important operational events: at least 1 year
- Audit / security events: long-term

Retention values remain configurable based on storage usage and operational needs.
