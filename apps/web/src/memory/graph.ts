// The layout of the Memory History Graph (UI_DESIGN.md §7: GitLens-like).
// One row per version, newest on top. The memory's own versions run down lane 0
// (the spine); each other memory reached through a relation (extends,
// conflicts_with, merged_from, ...) gets a lane of its own to the right, and the
// relation is drawn from the newer version to the older one.
import type { MemoryHistory, MemoryVersion, RelationType } from "./types";

export interface GraphNode {
  version: MemoryVersion;
  row: number;
  lane: number;
  /** A version of the memory on screen (not of a related memory). */
  own: boolean;
  /** The memory's current version (its highest number). */
  current: boolean;
}

export interface GraphEdge {
  from: GraphNode;
  to: GraphNode;
  relation: RelationType;
}

export interface GraphLayout {
  nodes: GraphNode[];
  edges: GraphEdge[];
  lanes: number;
  /** The rows of the first and last own version (the spine), or null. */
  spine: { top: number; bottom: number } | null;
}

function newestFirst(a: MemoryVersion, b: MemoryVersion): number {
  const byTime = Date.parse(b.created_at) - Date.parse(a.created_at);
  if (byTime !== 0 && !Number.isNaN(byTime)) return byTime;
  if (a.memory_id === b.memory_id) return b.version_number - a.version_number;
  return a.version_id < b.version_id ? -1 : 1;
}

export function layoutHistory(history: MemoryHistory): GraphLayout {
  const own = history.versions;
  const memoryId = own[0]?.memory_id ?? null;
  const currentNumber = own.reduce((max, version) => Math.max(max, version.version_number), 0);
  const seen = new Set<string>();
  const all: MemoryVersion[] = [];
  for (const version of [...own, ...history.related]) {
    if (seen.has(version.version_id)) continue;
    seen.add(version.version_id);
    all.push(version);
  }
  all.sort(newestFirst);

  const lanes = new Map<string, number>();
  if (memoryId) lanes.set(memoryId, 0);
  const nodes: GraphNode[] = all.map((version, row) => {
    let lane = lanes.get(version.memory_id);
    if (lane === undefined) {
      lane = lanes.size;
      lanes.set(version.memory_id, lane);
    }
    const isOwn = version.memory_id === memoryId;
    return {
      version,
      row,
      lane,
      own: isOwn,
      current: isOwn && version.version_number === currentNumber,
    };
  });

  const byId = new Map(nodes.map((node) => [node.version.version_id, node]));
  const edges: GraphEdge[] = [];
  const drawn = new Set<string>();
  for (const relation of history.relations) {
    const from = byId.get(relation.from_version_id);
    const to = byId.get(relation.to_version_id);
    const key = `${relation.from_version_id}>${relation.to_version_id}>${relation.relation}`;
    if (!from || !to || from === to || drawn.has(key)) continue;
    drawn.add(key);
    edges.push({ from, to, relation: relation.relation });
  }

  const ownRows = nodes.filter((node) => node.own).map((node) => node.row);
  return {
    nodes,
    edges,
    lanes: Math.max(lanes.size, 1),
    spine: ownRows.length > 0 ? { top: Math.min(...ownRows), bottom: Math.max(...ownRows) } : null,
  };
}
