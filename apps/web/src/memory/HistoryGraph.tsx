// The 履歴 tab (UI_DESIGN.md §7, the design's Memory board): the History Graph
// on the left and the selected version on the right. Restoring a past version
// writes a new active version with its content; the past version is never
// rewritten or made active again (Decision 0034, 2).
import { useState } from "react";
import { useI18n } from "../i18n";
import { DiffView } from "./DiffView";
import type { GraphEdge, GraphLayout, GraphNode } from "./graph";
import { actorLabel, freshnessTag, isExpired, stateChip } from "./labels";
import type { MemoryVersion } from "./types";

const ROW = 66;
const LANE = 34;
const X0 = 22;
const Y0 = 30;

const laneX = (lane: number) => X0 + lane * LANE;
const rowY = (row: number) => Y0 + row * ROW;

function edgePath(edge: GraphEdge): string {
  const x1 = laneX(edge.from.lane);
  const y1 = rowY(edge.from.row);
  const x2 = laneX(edge.to.lane);
  const y2 = rowY(edge.to.row);
  if (x1 === x2) return `M ${x1} ${y1} L ${x2} ${y2}`;
  // Leave the newer node's lane, curve over, and run down the older one's lane.
  const bend = Math.min(44, Math.abs(y2 - y1));
  const direction = y2 >= y1 ? 1 : -1;
  const mid = y1 + direction * bend;
  return `M ${x1} ${y1} C ${x1} ${y1 + direction * bend * 0.55}, ${x2} ${mid - direction * bend * 0.45}, ${x2} ${mid} L ${x2} ${y2}`;
}

function nodeClass(node: GraphNode): string {
  const version = node.version;
  if (node.current && version.status === "active") return "node current";
  if (version.status === "deprecated") return "node deprecated";
  // An unconfirmed candidate keeps its colour after it is superseded.
  if (version.confirmation_state === "inferred") return "node inferred";
  if (version.confirmation_state === "observed") return "node observed";
  if (version.status !== "active") return "node retired";
  return "node active";
}

/** The relation that links a related memory's version into this graph. */
function linkOf(node: GraphNode, edges: readonly GraphEdge[]): GraphEdge | undefined {
  return edges.find((edge) => edge.from === node || edge.to === node);
}

export function HistoryGraph({
  layout,
  selected,
  onSelect,
  selfId,
}: {
  layout: GraphLayout;
  selected: string | null;
  onSelect: (versionId: string) => void;
  selfId: string;
}) {
  const { t, formatDate } = useI18n();
  const height = Y0 + Math.max(layout.nodes.length - 1, 0) * ROW + 30;
  const width = laneX(layout.lanes - 1) + 14;
  return (
    <div className="memory-graph" style={{ height }}>
      <svg className="memory-graph-lines" width={width} height={height} aria-hidden="true">
        {layout.spine && layout.spine.bottom > layout.spine.top && (
          <path
            className="spine"
            d={`M ${X0} ${rowY(layout.spine.top)} L ${X0} ${rowY(layout.spine.bottom)}`}
          />
        )}
        {layout.edges.map((edge) => (
          <path
            key={`${edge.from.version.version_id}-${edge.to.version.version_id}-${edge.relation}`}
            className={`edge edge-${edge.relation}`}
            d={edgePath(edge)}
          />
        ))}
        {layout.nodes.map((node) => (
          <g key={node.version.version_id}>
            {node.version.version_id === selected && (
              <circle
                className="node-ring"
                cx={laneX(node.lane)}
                cy={rowY(node.row)}
                r={node.current ? 11 : 10}
              />
            )}
            <circle
              className={nodeClass(node)}
              cx={laneX(node.lane)}
              cy={rowY(node.row)}
              r={node.current ? 7 : node.own ? 6 : 5}
            />
          </g>
        ))}
      </svg>
      <ol className="memory-graph-rows" aria-label={t("memory.graph.label")}>
        {layout.nodes.map((node) => {
          const version = node.version;
          const state = t(stateChip(version).label);
          const link = node.own ? undefined : linkOf(node, layout.edges);
          const unconfirmed =
            version.confirmation_state !== "confirmed" && version.status !== "active";
          const title = node.own
            ? node.current && version.status === "active"
              ? t("memory.graph.current", { state })
              : unconfirmed
                ? t("memory.graph.retiredCandidate", {
                    state,
                    confirmation: t(`memory.confirmation.${version.confirmation_state}`),
                  })
                : state
            : version.title;
          const meta = node.own
            ? t("memory.graph.meta", {
                number: version.version_number,
                date: formatDate(version.created_at),
                who: actorLabel(version, selfId, t),
              })
            : t("memory.graph.relatedMeta", {
                relation: link?.relation ?? "",
                number: version.version_number,
                date: formatDate(version.created_at),
              });
          return (
            <li
              key={version.version_id}
              style={{ top: rowY(node.row) - 20, left: laneX(node.lane) + 20 }}
            >
              <button
                type="button"
                className={node.own ? "graph-row" : "graph-row related"}
                aria-pressed={version.version_id === selected}
                onClick={() => onSelect(version.version_id)}
              >
                <span className="graph-row-title ellipsis">
                  {node.own ? title : t("memory.graph.related", { title })}
                </span>
                <span className="graph-row-meta mono ellipsis">{meta}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function relationText(
  node: GraphNode,
  layout: GraphLayout,
  t: ReturnType<typeof useI18n>["t"],
): string {
  const name = (other: GraphNode) =>
    other.own
      ? `v${other.version.version_number}`
      : `${other.version.title} v${other.version.version_number}`;
  // Outgoing: "supersedes v2" (this one supersedes v2). Incoming: "v3 supersedes".
  const lines = layout.edges.flatMap((edge) => {
    if (edge.from === node) return [`${edge.relation} ${name(edge.to)}`];
    if (edge.to === node) return [`${name(edge.from)} ${edge.relation}`];
    return [];
  });
  if (lines.length === 0) return t("memory.none");
  return lines.join(", ");
}

function sameAudience(a: MemoryVersion, b: MemoryVersion): boolean {
  return (
    a.scope === b.scope &&
    a.owner_user_id === b.owner_user_id &&
    a.project_id === b.project_id &&
    a.project_group_id === b.project_group_id &&
    a.repo_id === b.repo_id
  );
}

/** The selected version: its text, who / why / relations / freshness, and restore. */
export function VersionCard({
  node,
  layout,
  current,
  selfId,
  restoring,
  onRestore,
}: {
  node: GraphNode;
  layout: GraphLayout;
  current: MemoryVersion;
  selfId: string;
  restoring: boolean;
  onRestore: (version: MemoryVersion) => void;
}) {
  const { t, formatDate } = useI18n();
  const [compare, setCompare] = useState(false);
  const version = node.version;
  const fresh = freshnessTag(version);
  // Restoring the active current version would change nothing (ALREADY_ACTIVE);
  // a related memory's version belongs to another memory.
  const canRestore =
    node.own &&
    // The Backend restores into an active or deprecated memory only (a memory
    // retired by another one stays retired), from a version of the same audience,
    // and not a freshness a person cannot write (session_only, or an expiry that
    // has passed: that needs a new freshness, which this screen does not ask for).
    (current.status === "active" || current.status === "deprecated") &&
    !(node.current && version.status === "active") &&
    sameAudience(version, current) &&
    version.freshness_policy !== "session_only" &&
    !isExpired(version);
  const canCompare = version.version_id !== current.version_id;
  return (
    <section className="memory-version" aria-labelledby="memory-version-title">
      <div className="memory-version-head">
        <span className={`version-dot ${node.current ? "current" : ""}`} aria-hidden="true" />
        <h3 id="memory-version-title">{t("memory.version.selected")}</h3>
        <span className="mono muted push-right">
          {t("memory.version.meta", {
            number: version.version_number,
            date: formatDate(version.created_at),
          })}
        </span>
      </div>
      {!node.own && <p className="small muted">{version.title}</p>}
      <p className="memory-version-text">{version.content}</p>
      <dl className="memory-version-facts">
        <div>
          <dt>{t("memory.version.actor")}</dt>
          <dd>{actorLabel(version, selfId, t)}</dd>
        </div>
        <div>
          <dt>{t("memory.version.reason")}</dt>
          <dd>{version.change_reason || t("memory.none")}</dd>
        </div>
        <div>
          <dt>{t("memory.version.relation")}</dt>
          <dd className="mono">{relationText(node, layout, t)}</dd>
        </div>
        <div>
          <dt>{t("memory.version.freshness")}</dt>
          <dd className={`tone-${fresh.tone}`}>{t(fresh.label)}</dd>
        </div>
      </dl>
      <div className="actions">
        {canRestore && (
          <button
            type="button"
            className="secondary small-button"
            disabled={restoring}
            onClick={() => onRestore(version)}
          >
            {restoring ? t("memory.version.restoring") : t("memory.version.restore")}
          </button>
        )}
        {canCompare && (
          <button
            type="button"
            className="text-button small-button"
            aria-expanded={compare}
            onClick={() => setCompare((value) => !value)}
          >
            {compare ? t("memory.version.hideCompare") : t("memory.version.compare")}
          </button>
        )}
      </div>
      {compare && canCompare && (
        <DiffView
          before={version.content}
          after={current.content}
          beforeLabel={t("memory.diff.version", { number: version.version_number })}
          afterLabel={t("memory.diff.current", { number: current.version_number })}
        />
      )}
      <p className="small muted">{t("memory.version.note")}</p>
    </section>
  );
}
