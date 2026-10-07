// エージェント / タスク (PAW-062; the design's Tasks, MobileTask and Tablet boards).
//   >= 768px  the task list (with the queue note) beside the selected task
//   < 768px   the list, then the task on its own screen (/agents/<id>) with the
//             steps in dependency order and the controls at the bottom
// The task, its DAG and its repositories come from the Backend through
// `TaskSource`; the controls are sent there and the Backend decides them.
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { isApiError } from "../api/client";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { Icon } from "../shell/icons";
import { DagGraph, NodeDetail } from "./DagGraph";
import {
  ACCEPTED_CONTROLS,
  approvalPath,
  type ControlCommand,
  type ControlOptions,
  type DagNode,
  decodeSegment,
  elapsed,
  isUnsettled,
  MAX_NAME_LENGTH,
  MAX_REASON_LENGTH,
  orderedNodes,
  REFRESH_MS,
  shortId,
  type TaskDetail,
  type TaskList,
  type TaskSummary,
  type ToolApproval,
} from "./model";
import { CheckMark, nodeTone, StatePill, taskTone, useNow, useTaskStateLabel } from "./parts";
import { type TaskSource, useTaskSource } from "./source";
import "./tasks.css";

export const TASKS_PATH = "/agents";

type Filter = "all" | "running" | "approval" | "failed";
const FILTERS: readonly Filter[] = ["all", "running", "approval", "failed"];

function matches(task: TaskSummary, filter: Filter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "running":
      return task.state === "running";
    case "approval":
      return task.state === "waiting" && task.waitReason === "approval";
    case "failed":
      return task.state === "failed";
  }
}

function isLive(task: TaskSummary): boolean {
  return task.state === "running" || task.state === "evaluating";
}

/** The id of `/agents/<id>`, or null for the list. */
function selectedTaskId(path: string): string | null {
  if (!path.startsWith(`${TASKS_PATH}/`)) return null;
  const id = path.slice(TASKS_PATH.length + 1);
  // A malformed escape is a task that does not exist, not a crash.
  return id ? (decodeSegment(id) ?? id) : null;
}

export function Unavailable({ title }: { title: MessageKey }) {
  const { t } = useI18n();
  return (
    <div className="page">
      <h1>{t(title)}</h1>
      <div className="info-box" role="status">
        <Icon name="info" size={16} />
        <div className="stack-xs">
          <strong className="small">{t("tasks.unavailable")}</strong>
          <p>{t("tasks.unavailableBody")}</p>
        </div>
      </div>
    </div>
  );
}

export function TasksPage() {
  const source = useTaskSource();
  if (!source) return <Unavailable title="tasks.title" />;
  return <TasksView source={source} />;
}

type Load<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: T };

function TasksView({ source }: { source: TaskSource }) {
  const { t } = useI18n();
  const { path } = useRouter();
  const selectedId = selectedTaskId(path);
  const [list, setList] = useState<Load<TaskList>>({ status: "loading" });
  const [filter, setFilter] = useState<Filter>("all");

  const reload = useCallback(() => {
    source
      .listTasks()
      .then((data) => setList({ status: "ready", data }))
      .catch((error: unknown) => setList({ status: "error", error }));
  }, [source]);
  useEffect(reload, [reload]);
  // A background read (polling, after a control): a failure keeps the last list.
  // A control's answer moves `controls` on: a read that started before it (a
  // poll in flight) must not put the card back (Codex review #174).
  const controls = useRef(0);
  const refreshList = useCallback(() => {
    const started = controls.current;
    source
      .listTasks()
      .then((data) => {
        if (controls.current === started) setList({ status: "ready", data });
      })
      .catch(() => {});
  }, [source]);
  // The task a control answered with replaces its card at once (the list's own
  // read may fail), then the list is read again.
  const applyControl = useCallback(
    (updated: TaskDetail) => {
      controls.current += 1;
      setList((current) =>
        current.status === "ready"
          ? {
              ...current,
              data: {
                ...current.data,
                tasks: current.data.tasks.map((task) =>
                  task.id === updated.id
                    ? {
                        ...task,
                        state: updated.state,
                        waitReason: updated.waitReason,
                        startedAt: updated.startedAt,
                        updatedAt: updated.updatedAt,
                      }
                    : task,
                ),
              },
            }
          : current,
      );
      refreshList();
    },
    [refreshList],
  );

  const tasks = list.status === "ready" ? list.data.tasks : [];
  // No push channel yet: the list is read again while it is shown (a task
  // changes, or a new one appears).
  const loaded = list.status === "ready";
  useEffect(() => {
    if (!loaded) return;
    const timer = window.setInterval(refreshList, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loaded, refreshList]);
  const visible = tasks.filter((task) => matches(task, filter));
  const now = useNow(tasks.some(isLive));
  // On a wide screen the first task is open until one is chosen; on a phone the
  // list stays alone (the layout hides the detail without a selection).
  const shownId = selectedId ?? visible[0]?.id ?? null;
  const capacity = list.status === "ready" ? list.data.capacity : undefined;

  return (
    <div className={selectedId ? "tasks-screen has-selection" : "tasks-screen"}>
      <div className="screen-head">
        <h1>{t("tasks.title")}</h1>
        <fieldset className="filter-chips">
          <legend className="visually-hidden">{t("tasks.filter.label")}</legend>
          {FILTERS.map((option) => {
            const count = tasks.filter((task) => matches(task, option)).length;
            return (
              <button
                key={option}
                type="button"
                className={`chip filter-${option}`}
                aria-pressed={filter === option}
                onClick={() => setFilter(option)}
              >
                {t(`tasks.filter.${option}`)}
                {option !== "all" && count > 0 && ` ${count}`}
              </button>
            );
          })}
        </fieldset>
        {capacity && (
          <span className="mono muted push-right capacity">
            {capacity.vramUsedGb !== undefined && capacity.vramTotalGb !== undefined
              ? t("tasks.capacityVram", {
                  limit: capacity.parallelLimit,
                  used: capacity.vramUsedGb,
                  total: capacity.vramTotalGb,
                })
              : t("tasks.capacity", { limit: capacity.parallelLimit })}
          </span>
        )}
      </div>
      <div className="tasks-body">
        <nav className="task-list-pane" aria-label={t("tasks.list")}>
          {list.status === "loading" && (
            <p className="muted small" role="status">
              {t("tasks.loading")}
            </p>
          )}
          {list.status === "error" && (
            <div className="stack">
              <p className="form-error" role="alert">
                {errorMessage(t, list.error)}
              </p>
              <button type="button" className="secondary small-button" onClick={reload}>
                {t("app.retry")}
              </button>
            </div>
          )}
          {list.status === "ready" && tasks.length === 0 && (
            <p className="muted small">{t("tasks.empty")}</p>
          )}
          {list.status === "ready" && tasks.length > 0 && visible.length === 0 && (
            <p className="muted small">{t("tasks.emptyFilter")}</p>
          )}
          <ul className="task-cards">
            {visible.map((task) => (
              <li key={task.id}>
                <TaskCard task={task} current={task.id === shownId} now={now} />
              </li>
            ))}
          </ul>
          <QueueNote tasks={tasks} />
        </nav>
        <div className="task-detail-pane">
          {shownId ? (
            <TaskDetailView key={shownId} source={source} id={shownId} onChanged={applyControl} />
          ) : (
            list.status === "ready" && <p className="muted small">{t("tasks.select")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

function TaskCard({ task, current, now }: { task: TaskSummary; current: boolean; now: number }) {
  const { formatTime } = useI18n();
  const label = useTaskStateLabel();
  const tone = taskTone(task.state, task.waitReason);
  const detail = isLive(task)
    ? elapsed(task.startedAt, null, now)
    : task.state === "waiting"
      ? label(task.state, task.waitReason)
      : formatTime(task.updatedAt);
  return (
    <Link
      to={`${TASKS_PATH}/${encodeURIComponent(task.id)}`}
      className="task-card"
      aria-current={current ? "page" : undefined}
    >
      <span className="task-card-line">
        <span className={`state-dot tone-${tone}`} aria-hidden="true" />
        <span className="task-card-title ellipsis">{task.title}</span>
        <span className="mono muted task-id">{shortId(task.id)}</span>
      </span>
      <span className="task-card-line">
        <span className={`state-tag tone-${tone}`}>{label(task.state, task.waitReason)}</span>
        <span className="mono muted ellipsis">
          {[task.repository, detail].filter(Boolean).join(" · ")}
        </span>
      </span>
    </Link>
  );
}

function QueueNote({ tasks }: { tasks: readonly TaskSummary[] }) {
  const { t } = useI18n();
  const waiting = tasks.filter(
    (task) => task.state === "waiting" && task.waitReason === "resource",
  );
  if (waiting.length === 0) return null;
  return (
    <div className="queue-note">
      <span className="section-label">{t("tasks.queue")}</span>
      {waiting.map((task) => (
        <p key={task.id}>
          {t("tasks.queueWaiting", {
            task: task.title,
            priority: t(`tasks.priority.${task.priority ?? "normal"}`),
          })}
        </p>
      ))}
    </div>
  );
}

function TaskDetailView({
  source,
  id,
  onChanged,
}: {
  source: TaskSource;
  id: string;
  onChanged: (task: TaskDetail) => void;
}) {
  const { t, formatTime } = useI18n();
  const [load, setLoad] = useState<Load<TaskDetail>>({ status: "loading" });
  const [pending, setPending] = useState<ControlCommand | null>(null);
  const [controlError, setControlError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  // Stop Now asks for the reason, Retry / Restart for another agent / model.
  const [form, setForm] = useState<ControlCommand | null>(null);

  const fetchTask = useCallback(() => {
    setLoad({ status: "loading" });
    source
      .getTask(id)
      .then((data) => setLoad({ status: "ready", data }))
      .catch((error: unknown) => setLoad({ status: "error", error }));
  }, [source, id]);
  useEffect(fetchTask, [fetchTask]);

  const task = load.status === "ready" ? load.data : null;
  const now = useNow(task !== null && isLive(task));
  // Read an unfinished task again in the background (its state, DAG, tool calls
  // and controls change without the operator); a failed refresh keeps what is shown.
  // A control moves `generation` on: a read that started before it (a poll in
  // flight) must not replace the task the control answered with.
  const generation = useRef(0);
  const refresh = useCallback(() => {
    const started = generation.current;
    source
      .getTask(id)
      .then((data) => {
        if (generation.current !== started) return;
        setLoad((current) =>
          current.status === "ready" && current.data.version > data.version
            ? current
            : { status: "ready", data },
        );
      })
      .catch(() => {});
  }, [source, id]);
  // A form of a control the task no longer accepts is closed for good (it must
  // not come back when the state accepts it again).
  useEffect(() => {
    if (form && task && !ACCEPTED_CONTROLS[task.state].includes(form)) setForm(null);
  }, [form, task]);
  // A failed or cancelled task is read again too: another tab or user may retry
  // or restart it (Codex review #174). Only a completed task is final.
  const polling =
    task !== null &&
    (isUnsettled(task.state) || ACCEPTED_CONTROLS[task.state].length > 0) &&
    pending === null;
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [polling, refresh]);

  if (load.status === "loading") {
    return (
      <p className="muted small" role="status">
        {t("tasks.loading")}
      </p>
    );
  }
  if (load.status === "error") {
    return (
      <div className="stack">
        <BackLink />
        <p className="form-error" role="alert">
          {isApiError(load.error, "not_found", "task_not_found")
            ? t("tasks.notFound")
            : errorMessage(t, load.error)}
        </p>
        <button type="button" className="secondary small-button" onClick={fetchTask}>
          {t("app.retry")}
        </button>
      </div>
    );
  }
  const data = load.data;

  const send = (command: ControlCommand, extra: Omit<ControlOptions, "expectedVersion"> = {}) => {
    generation.current += 1;
    setPending(command);
    setControlError(null);
    source
      .control(data.id, command, { ...extra, expectedVersion: data.version })
      .then((updated) => {
        generation.current += 1;
        setLoad({ status: "ready", data: updated });
        setForm(null);
        onChanged(updated);
      })
      .catch((error: unknown) => {
        setControlError(errorMessage(t, error));
        // The task may have changed meanwhile (a version conflict): show it as it is now.
        refresh();
      })
      .finally(() => setPending(null));
  };

  const nodes = data.dag ?? [];
  const node = nodes.find((entry) => entry.key === selectedNode) ?? null;
  const target = data.repositories.find((repo) => repo.role === "target") ?? data.repositories[0];
  const pullRequests = data.repositories.flatMap((repo) =>
    repo.pullRequest ? [repo.pullRequest] : [],
  );

  return (
    <article className="task-detail" aria-labelledby="task-title">
      <BackLink />
      <div className="task-title-block">
        <div className="task-title-line">
          <h2 id="task-title">{data.title}</h2>
          <span className="mono muted">{shortId(data.id)}</span>
          <StatePill state={data.state} waitReason={data.waitReason} />
        </div>
        <span className="mono muted task-context">
          {[
            [data.project, target?.name].filter(Boolean).join(" / "),
            target?.branch,
            isLive(data) ? elapsed(data.startedAt, null, now) : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </div>

      <TaskControls
        task={data}
        pending={pending}
        open={form}
        onSend={(command) =>
          command === "stop_now" || command === "retry" || command === "restart"
            ? setForm(form === command ? null : command)
            : send(command)
        }
      />
      {form && ACCEPTED_CONTROLS[data.state].includes(form) && (
        <ControlForm
          key={form}
          command={form}
          pending={pending !== null}
          onSubmit={(extra) => send(form, extra)}
          onClose={() => setForm(null)}
        />
      )}
      {controlError && (
        <p className="form-error task-control-error" role="alert">
          {controlError}
        </p>
      )}

      {data.state === "waiting" && data.waitReason === "approval" ? (
        <ApprovalNotice source={source} taskId={data.id} version={data.version} />
      ) : (
        data.state === "waiting" &&
        data.waitReason && (
          <div
            className={`wait-notice tone-${taskTone(data.state, data.waitReason)}`}
            role="status"
          >
            <Icon name="info" size={16} />
            <span>{t(`tasks.waitNotice.${data.waitReason}`)}</span>
          </div>
        )
      )}

      {data.currentStep && (
        <div className="current-work phone-only">
          <span className="section-label">{t("tasks.currentWork")}</span>
          <span className="strong">{data.currentStep.name}</span>
          <span className="tag-row">
            {data.agent && <span className="tag">{data.agent}</span>}
            {data.model && <span className="tag">{data.model}</span>}
            <span className="tag mono">
              {t("tasks.fact.attempt")} {data.attempt}
            </span>
          </span>
        </div>
      )}

      <dl className="task-facts">
        <Fact label={t("tasks.fact.agent")}>
          {[data.agent, data.model].filter(Boolean).join(" · ") || t("tasks.none")}
        </Fact>
        <Fact label={t("tasks.fact.step")}>
          {data.currentStep
            ? `${data.currentStep.name} · ${elapsed(data.currentStep.startedAt, null, now) ?? ""}`
            : t("tasks.none")}
        </Fact>
        <Fact label={t("tasks.fact.branch")}>{target?.branch ?? t("tasks.none")}</Fact>
        <Fact label={t("tasks.fact.worktree")}>{target?.worktree ?? t("tasks.none")}</Fact>
        <Fact label={t("tasks.fact.attempt")}>
          {t("tasks.attemptValue", { attempt: data.attempt, retries: data.retryCount })}
        </Fact>
        {data.budget && (
          <Fact label={t("tasks.fact.budget")}>
            {data.budget.remainingPercent !== undefined
              ? t("tasks.budgetRemaining", {
                  preset: t(`tasks.budget.${data.budget.preset}`),
                  percent: data.budget.remainingPercent,
                })
              : t(`tasks.budget.${data.budget.preset}`)}
          </Fact>
        )}
        {pullRequests.map((pr) => (
          <Link key={pr.id} to={`/pulls/${encodeURIComponent(pr.id)}`} className="fact-link">
            {t("tasks.openPr", { number: pr.number })}
          </Link>
        ))}
      </dl>

      {data.currentStep?.toolCalls && data.currentStep.toolCalls.length > 0 && (
        <section className="stack-xs step-tools" aria-labelledby="step-tools-title">
          <span id="step-tools-title" className="section-label">
            {t("tasks.node.toolCalls")} · {data.currentStep.name}
          </span>
          <ul className="plain-list">
            {data.currentStep.toolCalls.map((call) => (
              <li key={call.id} className="attempt-row">
                <span className="mono">{call.name}</span>
                <span className={`attempt-state status-${call.status}`}>
                  {t(`tasks.tool.${call.status}`)}
                </span>
                <span className="mono muted push-right">{formatTime(call.startedAt)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="task-card-panel dag-panel" aria-labelledby="dag-title">
        <div className="panel-head">
          <h3 id="dag-title">{t("tasks.dag.title")}</h3>
          {nodes.length > 0 && <span className="small muted">{t("tasks.dag.hint")}</span>}
        </div>
        {nodes.length === 0 ? (
          <p className="small muted">{t("tasks.dag.none")}</p>
        ) : (
          <>
            <div className="wide-only">
              <DagGraph
                nodes={nodes}
                taskLabel={data.title}
                selected={selectedNode}
                onSelect={setSelectedNode}
                now={now}
              />
            </div>
            <StepList nodes={nodes} selected={selectedNode} onSelect={setSelectedNode} now={now} />
            {node && (
              <NodeDetail
                node={node}
                nodes={nodes}
                now={now}
                onClose={() => setSelectedNode(null)}
              />
            )}
          </>
        )}
      </section>

      <RepositoryTable task={data} />
    </article>
  );
}

/**
 * A task waiting for approval (the MobileTask board): what the person is asked
 * to approve, with 確認 opening it (`/approvals/<id>`). Read again when the task
 * changes; without an approval for this person (another member's task, or it
 * was decided meanwhile) the plain notice stays.
 */
function ApprovalNotice({
  source,
  taskId,
  version,
}: {
  source: TaskSource;
  taskId: string;
  version: number;
}) {
  const { t } = useI18n();
  const [approval, setApproval] = useState<ToolApproval | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: read again for a new version
  useEffect(() => {
    let current = true;
    source
      .listApprovals(taskId)
      .then((items) => {
        if (current) setApproval(items[0] ?? null);
      })
      .catch(() => {
        if (current) setApproval(null);
      });
    return () => {
      current = false;
    };
  }, [source, taskId, version]);
  return (
    <div className="wait-notice tone-warning approval-notice" role="status">
      <Icon name="info" size={16} />
      <span>
        {approval
          ? t("tasks.approvalNeeded", { tool: approval.tool })
          : t("tasks.waitNotice.approval")}
      </span>
      {approval && (
        <Link className="approval-open" to={approvalPath(approval.id)}>
          {t("tasks.approvalOpen")}
        </Link>
      )}
    </div>
  );
}

function BackLink() {
  const { t } = useI18n();
  return (
    <Link to={TASKS_PATH} className="back-row phone-only">
      <Icon name="back" size={18} />
      {t("tasks.back")}
    </Link>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd className="mono">{children}</dd>
    </div>
  );
}

function TaskControls({
  task,
  pending,
  open,
  onSend,
}: {
  task: TaskDetail;
  pending: ControlCommand | null;
  open: ControlCommand | null;
  onSend: (command: ControlCommand) => void;
}) {
  const { t } = useI18n();
  const commands = ACCEPTED_CONTROLS[task.state];
  if (commands.length === 0) return null;
  return (
    <fieldset className="task-controls">
      <legend className="visually-hidden">{t("tasks.control.label")}</legend>
      {commands.map((command, index) => (
        <button
          key={command}
          type="button"
          className={
            command === "stop_now"
              ? "danger small-button"
              : index === 0
                ? "secondary small-button"
                : "text-button bordered small-button"
          }
          disabled={pending !== null}
          aria-busy={pending === command}
          aria-expanded={
            command === "stop_now" || command === "retry" || command === "restart"
              ? open === command
              : undefined
          }
          onClick={() => onSend(command)}
        >
          {command === "stop_now" ? (
            <>
              <span className="wide-label">{t("tasks.control.stop_now")}</span>
              <span className="phone-label" aria-hidden="true">
                {t("tasks.control.stop_now.short")}
              </span>
            </>
          ) : (
            t(`tasks.control.${command}`)
          )}
        </button>
      ))}
    </fieldset>
  );
}

/**
 * The arguments a control needs before it is sent: Stop Now the operator's
 * reason (required by the Backend and kept in the audit), Retry / Restart an
 * optional other agent or model (empty keeps the current ones).
 */
function ControlForm({
  command,
  pending,
  onSubmit,
  onClose,
}: {
  command: ControlCommand;
  pending: boolean;
  onSubmit: (extra: Omit<ControlOptions, "expectedVersion">) => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [reason, setReason] = useState("");
  const [agent, setAgent] = useState("");
  const [model, setModel] = useState("");
  const stop = command === "stop_now";
  const label = t(`tasks.control.${command}`);
  return (
    <form
      className={stop ? "control-form danger-form" : "control-form"}
      aria-label={label}
      onSubmit={(event) => {
        event.preventDefault();
        if (stop) {
          if (reason.trim()) onSubmit({ reason: reason.trim() });
          return;
        }
        onSubmit({
          ...(agent.trim() ? { agent: agent.trim() } : {}),
          ...(model.trim() ? { model: model.trim() } : {}),
        });
      }}
    >
      <p className="small muted">{t(stop ? "tasks.form.stopHint" : "tasks.form.agentHint")}</p>
      {stop ? (
        <label className="field">
          <span>{t("tasks.form.reason")}</span>
          <textarea
            required
            maxLength={MAX_REASON_LENGTH}
            rows={2}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      ) : (
        <div className="control-form-row">
          <label className="field">
            <span>{t("tasks.form.agent")}</span>
            <input
              value={agent}
              maxLength={MAX_NAME_LENGTH}
              onChange={(event) => setAgent(event.target.value)}
            />
          </label>
          <label className="field">
            <span>{t("tasks.form.model")}</span>
            <input
              value={model}
              maxLength={MAX_NAME_LENGTH}
              onChange={(event) => setModel(event.target.value)}
            />
          </label>
        </div>
      )}
      <div className="actions">
        <button
          type="submit"
          className={stop ? "danger small-button" : "small-button"}
          disabled={pending || (stop && !reason.trim())}
        >
          {t("tasks.form.submit", { command: label })}
        </button>
        <button type="button" className="text-button small-button" onClick={onClose}>
          {t("tasks.form.close")}
        </button>
      </div>
    </form>
  );
}

/** The phone's steps: the DAG's nodes in dependency order. */
function StepList({
  nodes,
  selected,
  onSelect,
  now,
}: {
  nodes: readonly DagNode[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  now: number;
}) {
  const { t } = useI18n();
  return (
    <ol className="step-list phone-only" aria-label={t("tasks.steps")}>
      {orderedNodes(nodes).map((node) => {
        const attempt = node.attempts[node.attempts.length - 1];
        return (
          <li key={node.key}>
            <button
              type="button"
              className={`step state-${node.state} tone-${nodeTone(node.state)}`}
              aria-pressed={selected === node.key}
              onClick={() => onSelect(selected === node.key ? null : node.key)}
            >
              <span className="step-icon" aria-hidden="true">
                {node.state === "succeeded" && <CheckMark />}
              </span>
              <span className="step-title">{node.title}</span>
              <span className="visually-hidden">{t(`tasks.node.${node.state}`)}</span>
              <span className="mono muted">
                {attempt
                  ? (elapsed(attempt.startedAt, attempt.finishedAt, now) ?? "")
                  : t("tasks.none")}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function RepositoryTable({ task }: { task: TaskDetail }) {
  const { t } = useI18n();
  if (task.repositories.length === 0) return null;
  return (
    <section className="task-card-panel repo-panel" aria-label={t("tasks.repo.title")}>
      <div className="repo-grid repo-head" aria-hidden="true">
        <span>{t("tasks.repo.name")}</span>
        <span>{t("tasks.repo.role")}</span>
        <span>{t("tasks.repo.branch")}</span>
        <span>{t("tasks.repo.tests")}</span>
        <span>{t("tasks.repo.review")}</span>
        <span>{t("tasks.repo.pr")}</span>
      </div>
      <ul className="plain-list">
        {task.repositories.map((repo) => {
          const readOnly = repo.role === "referenced";
          return (
            <li key={repo.name} className="repo-grid repo-row">
              <span className="strong">{repo.name}</span>
              <span className={`role-tag role-${repo.role}`}>{t(`tasks.role.${repo.role}`)}</span>
              <span className="mono muted ellipsis">
                {readOnly
                  ? `${repo.branch ?? ""}（${t("tasks.repo.readOnly")}）`
                  : (repo.branch ?? t("tasks.none"))}
              </span>
              <span className={`result result-${repo.evaluation}`}>
                <span className="phone-label">{t("tasks.repo.tests")} </span>
                {readOnly ? t("tasks.none") : t(`tasks.evaluation.${repo.evaluation}`)}
              </span>
              <span className={`result review-${repo.review}`}>
                <span className="phone-label">{t("tasks.repo.review")} </span>
                {readOnly ? t("tasks.none") : t(`tasks.review.${repo.review}`)}
              </span>
              <span className="muted">
                {repo.pullRequest ? (
                  <Link to={`/pulls/${encodeURIComponent(repo.pullRequest.id)}`}>
                    #{repo.pullRequest.number} {t(`tasks.pr.${repo.pullRequest.state}`)}
                  </Link>
                ) : (
                  t("tasks.none")
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
