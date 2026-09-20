# Notification Policy

更新日: 2026-09-16

## 1. 基本方針

Personal AI Workspaceでは、通常の警告・お知らせ・軽微な失敗・状態変化を
ベルアイコンのNotification Centerへ集約する。

作業中のユーザーを不要なPopupやModalで遮らないことを重視する。

## 2. Severity

### INFO
Notification Centerのみを基本とする。

例:
- Task完了
- Memory整理完了
- Repo解析完了
- 軽微な状態変更

### WARNING
Bell badge + Notification Centerを基本とする。

例:
- Backup一時失敗
- Retry中
- Resource使用率上昇
- Revalidate候補

### ERROR
Notification Centerで強調表示する。
必要に応じて非モーダルBannerを許可する。

例:
- Backup連続失敗
- Agent Job停止
- Memory Consolidation失敗
- 外部Service接続失敗

### CRITICAL
画面上部Banner / Modal等の強い表示を許可する。

例:
- データ損失の可能性
- Security異常
- 権限異常
- DB書き込み障害
- 長時間Backup不能
- Ownerの対応が必要な重大障害

Modalは、ユーザーが即時判断しなければ安全性・データ整合性に影響する場合に限定する。

## 3. Notification Center

最低限以下を持つ。

- 未読件数
- Severity
- Timestamp
- Source subsystem
- Project / User context
- Message
- Related resource
- Action
- Read / Unread
- Dismissed state

Filter:
- Severity
- System
- Project
- User
- Unread

## 4. Noise Reduction

同種通知は集約する。

例:

```text
Memory Backupが5回連続で失敗しています
最終成功: 10:30
最新失敗: 13:00
```

Retry中の一時的な失敗は、即座に大きなUI警告へ昇格させない。

## 5. Role-aware Notification

Owner / Admin専用:
- Backup
- Global model/runtime
- User management
- Security
- Resource quota
- System health

一般User:
- 自分のTask
- 自分のProject
- 自分のMemory
- 自分に影響するService障害

## 6. Actions

通知から直接実行可能なAction例:

- 詳細を見る
- 再試行
- 関連設定を開く
- 既読
- Taskを開く
- Audit Logを開く

Action自体が高リスク操作の場合は、通常のPermission / Step-up Auth Policyに従う。



## 7. Memory Backupへの適用

Memory Markdown Backupは変更がある場合のみ30分ごとに実行する。

一時失敗はWARNINGとしてNotification Centerへ通知し、自動Retryする。
連続失敗はERROR、長時間未成功やデータ保全上重大な状態はCRITICALへ昇格する。

通常時はBell / Badge中心とし、大きなBannerやModalは即時対応が必要な場合に限定する。
