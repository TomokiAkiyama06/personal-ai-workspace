// The dependency graph of a task (the design's Tasks board: 依存グラフ). Nodes are
// buttons laid out by dependency depth over an SVG layer of arrows; selecting a
// node opens its agent, tool calls and attempts below the graph.
import { useI18n } from "../i18n";
import { Icon } from "../shell/icons";
import { type DagNode, elapsed, layoutDag, type NodeAttempt } from "./model";
import { nodeTone } from "./parts";

const NODE_WIDTH = 176;
const NODE_HEIGHT = 66;
const COLUMN_PITCH = 190;
const ROW_PITCH = 116;
const PADDING = 8;
const ARROW = 5;

function lastAttempt(node: DagNode): NodeAttempt | undefined {
  return node.attempts[node.attempts.length - 1];
}

/** "Codex · 高" / "Local · qwen3": who runs the node, else its role. */
function useNodeAgent(): (node: DagNode) => string {
  const { t } = useI18n();
  return (node) => {
    const attempt = lastAttempt(node);
    const agent = attempt?.agent ?? node.agent;
    const model = attempt?.model ?? node.model;
    if (agent && model) return `${agent} · ${model}`;
    return agent ?? t(`tasks.role.${node.role}`);
  };
}

export function DagGraph({
  nodes,
  taskLabel,
  selected,
  onSelect,
  now,
}: {
  nodes: readonly DagNode[];
  taskLabel: string;
  selected: string | null;
  onSelect: (key: string | null) => void;
  now: number;
}) {
  const { t } = useI18n();
  const agentOf = useNodeAgent();
  const positions = layoutDag(nodes);
  const columns = Math.max(1, ...positions.map((position) => position.column + 1));
  const rows = Math.max(1, ...positions.map((position) => position.row + 1));
  const width = PADDING * 2 + (columns - 1) * COLUMN_PITCH + NODE_WIDTH;
  const height = PADDING * 2 + (rows - 1) * ROW_PITCH + NODE_HEIGHT;
  const at = new Map(
    positions.map((position) => [
      position.node.key,
      {
        x: PADDING + position.column * COLUMN_PITCH,
        y: PADDING + position.row * ROW_PITCH,
      },
    ]),
  );

  const edges: { key: string; d: string }[] = [];
  for (const { node } of positions) {
    const to = at.get(node.key);
    if (!to) continue;
    for (const parentKey of node.dependsOn) {
      const from = at.get(parentKey);
      if (!from) continue;
      const x1 = from.x + NODE_WIDTH;
      const y1 = from.y + NODE_HEIGHT / 2;
      const x2 = to.x - ARROW;
      const y2 = to.y + NODE_HEIGHT / 2;
      const middle = to.x - (COLUMN_PITCH - NODE_WIDTH) / 2;
      edges.push({
        key: `${parentKey}->${node.key}`,
        d:
          y1 === y2
            ? `M ${x1} ${y1} L ${x2} ${y2}`
            : `M ${x1} ${y1} L ${middle} ${y1} L ${middle} ${y2} L ${x2} ${y2}`,
      });
    }
  }

  return (
    <div className="dag-scroll">
      <div className="dag" style={{ width, height }}>
        <svg
          className="dag-edges"
          width={width}
          height={height}
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <marker
              id="dag-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 9 5 L 0 9 z" fill="currentColor" />
            </marker>
          </defs>
          {edges.map((edge) => (
            <path key={edge.key} d={edge.d} markerEnd="url(#dag-arrow)" />
          ))}
        </svg>
        <ul className="dag-nodes" aria-label={t("tasks.dag.label", { task: taskLabel })}>
          {positions.map(({ node }) => {
            const point = at.get(node.key) ?? { x: 0, y: 0 };
            const attempt = lastAttempt(node);
            const duration = attempt ? elapsed(attempt.startedAt, attempt.finishedAt, now) : null;
            const status = t(`tasks.node.${node.state}`);
            return (
              <li key={node.key} style={{ left: point.x, top: point.y }}>
                <button
                  type="button"
                  className={`dag-node tone-${nodeTone(node.state)} state-${node.state}`}
                  aria-pressed={selected === node.key}
                  onClick={() => onSelect(selected === node.key ? null : node.key)}
                >
                  <span className="dag-node-title">
                    <span className="state-dot" aria-hidden="true" />
                    <span className="ellipsis">{node.title}</span>
                  </span>
                  <span className="dag-node-meta ellipsis">{agentOf(node)}</span>
                  <span className="dag-node-meta">
                    {duration && node.state !== "pending" ? `${status} ${duration}` : status}
                    {!node.required && ` · ${t("tasks.dag.optional")}`}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

export function NodeDetail({
  node,
  nodes,
  now,
  onClose,
}: {
  node: DagNode;
  nodes: readonly DagNode[];
  now: number;
  onClose: () => void;
}) {
  const { t, formatTime } = useI18n();
  const agentOf = useNodeAgent();
  const titles = node.dependsOn.map(
    (key) => nodes.find((other) => other.key === key)?.title ?? key,
  );
  return (
    <section className="node-detail" aria-label={node.title}>
      <div className="node-detail-head">
        <span className={`state-pill tone-${nodeTone(node.state)}`}>
          <span className="state-dot" aria-hidden="true" />
          {t(`tasks.node.${node.state}`)}
        </span>
        <h4>{node.title}</h4>
        <span className="mono muted">
          {t(`tasks.role.${node.role}`)} · {agentOf(node)}
        </span>
        <button
          type="button"
          className="icon-button push-right"
          aria-label={t("tasks.node.close")}
          onClick={onClose}
        >
          <Icon name="back" size={16} />
        </button>
      </div>
      {titles.length > 0 && (
        <p className="small muted">{t("tasks.dag.dependsOn", { nodes: titles.join("、") })}</p>
      )}
      <div className="stack-xs">
        <span className="section-label">{t("tasks.node.attempts")}</span>
        {node.attempts.length === 0 ? (
          <p className="small muted">{t("tasks.node.noAttempts")}</p>
        ) : (
          <ul className="plain-list">
            {node.attempts.map((attempt) => (
              <li key={attempt.number} className="attempt-row">
                <span className="strong small">
                  {t("tasks.node.attemptNumber", { number: attempt.number })}
                </span>
                <span className={`attempt-state status-${attempt.state}`}>
                  {t(`tasks.attempt.${attempt.state}`)}
                </span>
                <span className="mono muted">
                  {[attempt.agent, attempt.model].filter(Boolean).join(" · ")}
                  {attempt.placement && ` · ${t(`tasks.placement.${attempt.placement}`)}`}
                </span>
                {attempt.errorClass && (
                  <span className="mono error-text">{attempt.errorClass}</span>
                )}
                <span className="mono muted push-right">
                  {formatTime(attempt.startedAt)} ·{" "}
                  {elapsed(attempt.startedAt, attempt.finishedAt, now) ?? t("tasks.none")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
