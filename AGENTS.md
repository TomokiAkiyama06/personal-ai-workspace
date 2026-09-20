# AGENTS.md — Personal AI Workspace

更新日: 2026-09-20

このファイルはLocal / Codex / Claude Code等のコーディングエージェントに対する共通ルールの正本とする。

## 0. Repository development mode

このRepository自体の初期開発では **CodexをPrimary Implementation Agent** とする。

- Codex: 主実装、Bug fix、Test、Refactor、PR作成
- Claude Code: 独立Review、Architecture、Security、Requirement alignment
- Evaluator: 機械的な成功判定
- Human: 最終権限

`Local-first` はPersonal AI Workspace製品のRuntime方針であり、
BenchmarkでLocal Coding Modelが選定される前のRepository開発担当をLocalへ固定する意味ではない。

初期開発の標準フロー:

```text
Issue / Task
→ REQUIREMENTS / AGENTS / related docs確認
→ Codex implementation
→ Codex tests
→ Evaluator
→ Claude review
→ 必要ならCodex修正
→ PR
→ Merge Ready
→ Human decision
```

人間が現在のTask/Sessionで「問題なければマージして」等を明示した場合のみ、
そのTaskに限ってCodexが最終Mergeまで進めてよい。

## 1. 基本原則
1. Local-first。
2. 品質を犠牲にしてまでLocalに固執しない。
3. Agent自身の「完了」「自信」は成功判定に使わない。
4. Evidence / Test / Acceptance Criteriaを優先する。
5. 最終Merge権限は原則人間が保持する。
6. 人間の明示指示なしにPRをMergeしない。
7. Agent/Modelは交換可能な実行エンジンとして扱う。
8. 仕様にない重要判断を勝手に確定しない。
9. Global PolicyはAdminだけが変更できる。
10. 他ユーザーのPrivate Memory / Credential / Workspaceへアクセスしない。

## 2. 呼称
- Local: GPUサーバー上のローカルAgent
- サブスク: Codex + Claude Code
- Evaluator: Agentから独立した機械的検証層
- Human: 最終責任者
- Admin: Global Policyを変更できる人間

## 3. 標準フロー
```text
Task
→ Requirements/Context取得
→ Codex implementation（初期開発）
→ Tests
→ Evaluator
→ Claude / independent review
→ 必要ならCodex修正
→ Evaluator再実行
→ PR
→ Merge Ready
→ Human decision
```

Benchmark後、採用Local Coding Modelが十分な品質を満たした場合は、
Task種別に応じてLocal WorkerをPrimary / Parallel Workerとして利用できる。


## 4. Merge Policy
通常AgentはMerge禁止。

人間が現在のTask/Session内で以下の趣旨を明示した場合のみ実行可:
- マージしてください
- 問題なければマージしてください
- マージまで進めてください
- マージしながら作業してください

CI PASS、Review APPROVE、以前の許可は現在TaskのMerge許可を意味しない。

## 5. Git / Worktree
複数Agentを同一Working Treeで同時実行しない。並列はworktreeで分離する。
Reviewerは原則Read-only。指摘は実装側へ返す。
Force push / branch delete / history rewriteは明示許可なしに実行しない。

## 6. Evaluation
完了条件:
- 要求を満たす
- Build
- 既存Test
- Issue-specific test
- Regression
- Hidden test（存在時）
- Security findingなし

テスト削除・無効化・期待値の不当緩和でPASSさせない。

## 7. Bug Fix
`Reproduce → Root Cause → Fix → Same Reproduction → Regression`
修正前に再現できない場合は明示する。

## 8. Local
通常実装の第一候補。
- 実装
- Bug fix
- Test
- Repo探索
- Refactor
- Docs
- Review指摘への対応

同じ失敗を繰り返したら無限ループせずエスカレーション。

## 9. サブスク
Codex:
- correctness
- test
- bug
- type/exception
- 高難度実装

Claude Code:
- architecture
- requirement alignment
- security
- maintainability
- ambiguity

役割は固定せず実測で更新可能。

## 10. 最新情報
最新ライブラリ/API/規約/仕様が必要なタスクでは古いローカル知識を断定利用しない。
Freshness requiredなら最新情報取得可能な経路へ上げる。
調査結果だけサブスクで取得し、Localへ戻して実装させてもよい。

## 11. Memory
MemoryはWorkspace所有。
他ユーザーPrivate Memoryを探索しない。
RepoのSource of TruthはGit。古いMemoryより現在Repoを優先。

## 12. Security
- Credentialをログへ出さない
- Secretをcommitしない
- 他ユーザーHOMEへアクセスしない
- User権限でAdmin APIを呼ばない
- Global System Promptを変更しない
- Audit Logを改変しない
- 明示許可なしにMergeしない

## 13. Audit
各Task:
- user
- project
- repo
- branch
- agent/model
- start/end
- files changed
- tests
- tool calls
- result
- escalation
- PR

## 14. 仕様変更
重要判断は `docs/decisions/NNNN-<slug>.md` に提案し、人間/Admin承認を得る。
既存Decisionを直接書き換えず、変更は新Decisionから`Supersedes`する。



## GitHub merge behavior

GitHub側で複数のmerge methodが有効でも、Agentは自動的にMerge権限を持たない。

通常:
- implementation
- tests
- push
- PR creation
- review response
- Merge Ready

までを行う。

UserがTask / Session内で明示的に「問題なかったらマージして」等と許可した場合のみ、
そのTask / Sessionに限ってMergeまで進めてよい。

GitHub repository settingのcapabilityと、Agent authorizationを混同しない。
