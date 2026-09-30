// メモリ (PAW-063, the PAW-060 design canvas "Memory" board; UI_DESIGN.md §6-8, 17).
// Three panes: Scope Tree / Memory List / Memory Detail. Below 1280px the scope
// tree becomes a select above the list; below 768px the list and the detail are
// two levels (/memory and /memory/<id>). The Backend decides what the reader may
// see and do; this screen only shows it.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { Link, useRouter } from "../router";
import { useDismiss } from "../shell/common";
import { freshnessTag, needsReview, stateChip } from "./labels";
import { LoadError, MemoryDetail } from "./MemoryDetail";
import { type MemorySource, useMemorySource } from "./source";
import type { MemoryVersion, ScopeKey, ScopeTree } from "./types";
import { useLoad } from "./useLoad";
import "./memory.css";

type StatusFilter = "all" | "active" | "retired";
const STATUS_FILTERS: readonly StatusFilter[] = ["all", "active", "retired"];

function scopeValue(scope: ScopeKey): string {
  switch (scope.kind) {
    case "user":
    case "shared":
      return scope.kind;
    case "project":
      return `project:${scope.project_id}`;
    case "repo":
      return `repo:${scope.project_id}:${scope.repo_id}`;
  }
}

function scopeFromValue(value: string): ScopeKey {
  const [kind, project, repo] = value.split(":");
  if (kind === "project" && project) return { kind: "project", project_id: project };
  if (kind === "repo" && project && repo) {
    return { kind: "repo", project_id: project, repo_id: repo };
  }
  return kind === "shared" ? { kind: "shared" } : { kind: "user" };
}

/** The memory id of /memory/<id>, or null on /memory. */
function selectedId(path: string): string | null {
  const match = /^\/memory\/([^/]+)\/?$/.exec(path);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function PinIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinejoin="round"
      aria-hidden="true"
      className="memory-pin"
    >
      <path d="M9 3h6l-1 6 4 3v2H6v-2l4-3Z" />
      <path d="M12 14v7" />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={open ? "chevron" : "chevron closed"}
    >
      <path d="m6 9.5 6 5.5 6-5.5" />
    </svg>
  );
}

function ScopeRow({
  label,
  count,
  active,
  indent,
  dot,
  onSelect,
}: {
  label: string;
  count: number;
  active: boolean;
  indent?: boolean;
  dot?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={indent ? "scope-row indent" : "scope-row"}
      aria-current={active ? "true" : undefined}
      onClick={onSelect}
    >
      {dot && <span className="scope-dot" aria-hidden="true" />}
      <span className="scope-name ellipsis">{label}</span>
      <span className="scope-count mono">{count}</span>
    </button>
  );
}

function ScopePane({
  tree,
  scope,
  onSelect,
}: {
  tree: ScopeTree;
  scope: ScopeKey;
  onSelect: (scope: ScopeKey) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<ReadonlySet<string>>(
    () => new Set(tree.projects.map((project) => project.project_id)),
  );
  const current = scopeValue(scope);
  const toggle = (id: string) =>
    setOpen((value) => {
      const next = new Set(value);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <nav className="memory-scopes" aria-label={t("memory.scopes")}>
      <div className="memory-scopes-head">
        <span className="section-label">{t("memory.scopes")}</span>
        <button
          type="button"
          className="text-button memory-collapse"
          onClick={() => setOpen(new Set())}
        >
          {t("memory.scopes.collapseAll")}
        </button>
      </div>
      <ScopeRow
        label={t("memory.scope.user")}
        count={tree.user}
        active={current === "user"}
        onSelect={() => onSelect({ kind: "user" })}
      />
      {tree.projects.map((project) => {
        const expanded = open.has(project.project_id);
        return (
          <div key={project.project_id} className="scope-group">
            <button
              type="button"
              className="scope-row"
              aria-expanded={expanded}
              onClick={() => toggle(project.project_id)}
            >
              <Chevron open={expanded} />
              <span className="scope-name ellipsis">{project.name}</span>
              <span className="scope-count mono">{project.count}</span>
            </button>
            {expanded && (
              <>
                <ScopeRow
                  indent
                  dot
                  label={t("memory.scope.projectCommon")}
                  count={project.project_count}
                  active={current === `project:${project.project_id}`}
                  onSelect={() => onSelect({ kind: "project", project_id: project.project_id })}
                />
                {project.repos.map((repo) => (
                  <ScopeRow
                    key={repo.repo_id}
                    indent
                    label={repo.name}
                    count={repo.count}
                    active={current === `repo:${project.project_id}:${repo.repo_id}`}
                    onSelect={() =>
                      onSelect({
                        kind: "repo",
                        project_id: project.project_id,
                        repo_id: repo.repo_id,
                      })
                    }
                  />
                ))}
              </>
            )}
          </div>
        );
      })}
      <ScopeRow
        label={t("memory.scope.shared")}
        count={tree.shared}
        active={current === "shared"}
        onSelect={() => onSelect({ kind: "shared" })}
      />
    </nav>
  );
}

/** The scope pane as a select, for the narrower layouts. */
function ScopeSelect({
  tree,
  scope,
  onSelect,
}: {
  tree: ScopeTree;
  scope: ScopeKey;
  onSelect: (scope: ScopeKey) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="memory-scope-select">
      <label htmlFor={id} className="visually-hidden">
        {t("memory.scope.select")}
      </label>
      <select
        id={id}
        value={scopeValue(scope)}
        onChange={(event) => onSelect(scopeFromValue(event.target.value))}
      >
        <option value="user">{t("memory.scope.user")}</option>
        {tree.projects.map((project) => (
          <optgroup key={project.project_id} label={project.name}>
            <option value={`project:${project.project_id}`}>
              {t("memory.scope.projectCommon")}
            </option>
            {project.repos.map((repo) => (
              <option key={repo.repo_id} value={`repo:${project.project_id}:${repo.repo_id}`}>
                {repo.name}
              </option>
            ))}
          </optgroup>
        ))}
        <option value="shared">{t("memory.scope.shared")}</option>
      </select>
    </div>
  );
}

function StatusMenu({
  value,
  onChange,
}: {
  value: StatusFilter;
  onChange: (value: StatusFilter) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, container, close);
  return (
    <div className="memory-status" ref={container}>
      <button
        type="button"
        className="chip"
        aria-expanded={open}
        aria-controls={menuId}
        aria-pressed={value !== "all"}
        onClick={() => setOpen((current) => !current)}
      >
        {value === "all"
          ? t("memory.filter.status")
          : t("memory.filter.statusWith", { value: t(`memory.filter.${value}`) })}
      </button>
      {open && (
        <div id={menuId} className="popover memory-status-menu">
          <fieldset className="filter-chips">
            <legend className="visually-hidden">{t("memory.filter.status")}</legend>
            {STATUS_FILTERS.map((option) => (
              <button
                key={option}
                type="button"
                className="chip"
                aria-pressed={value === option}
                onClick={() => {
                  onChange(option);
                  close();
                }}
              >
                {t(`memory.filter.${option}`)}
              </button>
            ))}
          </fieldset>
        </div>
      )}
    </div>
  );
}

function scopeLabel(version: MemoryVersion, tree: ScopeTree | null): string {
  if (version.scope === "repo" && version.repo_id) {
    for (const project of tree?.projects ?? []) {
      const repo = project.repos.find((entry) => entry.repo_id === version.repo_id);
      if (repo) return `repo/${repo.name}`;
    }
    return "repo";
  }
  return version.scope;
}

function MemoryList({
  items,
  tree,
  selected,
}: {
  items: MemoryVersion[];
  tree: ScopeTree | null;
  selected: string | null;
}) {
  const { t, locale } = useI18n();
  // Updated (UI_DESIGN.md §6.2): the day the current version was written.
  const day = (iso: string) => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? iso
      : new Intl.DateTimeFormat(locale === "ja" ? "ja-JP" : "en-US", {
          dateStyle: "medium",
        }).format(date);
  };
  return (
    <ul className="memory-list" aria-label={t("memory.list.label")}>
      {items.map((version) => {
        const chip = stateChip(version);
        const fresh = freshnessTag(version);
        return (
          <li key={version.memory_id}>
            <Link
              to={`/memory/${encodeURIComponent(version.memory_id)}`}
              className="memory-card"
              aria-current={version.memory_id === selected ? "page" : undefined}
            >
              <span className="memory-card-title">
                {version.pinned && (
                  <>
                    <PinIcon />
                    <span className="visually-hidden">{t("memory.list.pinned")}</span>
                  </>
                )}
                <span className="ellipsis">{version.title}</span>
              </span>
              <span className="memory-card-meta">
                <span className={`memory-state tone-${chip.tone}`}>{t(chip.label)}</span>
                <span className="mono muted ellipsis">
                  {scopeLabel(version, tree)} · {t(fresh.label)}
                </span>
                <time className="mono muted memory-card-date" dateTime={version.created_at}>
                  {day(version.created_at)}
                </time>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function MemoryUnavailable() {
  const { t } = useI18n();
  return (
    <div className="page">
      <h1>{t("memory.title")}</h1>
      <div className="info-box memory-unavailable">
        <p>{t("memory.unavailable")}</p>
      </div>
    </div>
  );
}

function MemoryScreen({ source }: { source: MemorySource }) {
  const { t } = useI18n();
  const { path } = useRouter();
  const selected = selectedId(path);
  const searchId = useId();
  const [scope, setScope] = useState<ScopeKey>({ kind: "user" });
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [review, setReview] = useState(false);

  // Search as the reader types, once the typing pauses.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [input]);

  const tree = useLoad(() => source.scopes(), [source]);
  const scopeKey = scopeValue(scope);
  const list = useLoad(() => source.list(scope, query), [source, scopeKey, query]);
  const reloadTree = tree.reload;
  const reloadList = list.reload;
  const onChanged = useCallback(() => {
    reloadTree();
    reloadList();
  }, [reloadTree, reloadList]);

  const items = list.data ?? [];
  const reviewCount = items.filter((version) => needsReview(version)).length;
  const shown = useMemo(
    () =>
      items.filter((version) => {
        if (review && !needsReview(version)) return false;
        if (status === "active") return version.status === "active";
        if (status === "retired") return version.status !== "active";
        return true;
      }),
    [items, review, status],
  );

  return (
    <div className={selected ? "memory-screen has-selection" : "memory-screen"}>
      <div className="memory-toolbar">
        <h1>{t("memory.title")}</h1>
        <div className="memory-search">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="6" />
            <path d="M15.5 15.5 20 20" />
          </svg>
          <label htmlFor={searchId} className="visually-hidden">
            {t("memory.search")}
          </label>
          <input
            id={searchId}
            type="search"
            placeholder={t("memory.searchPlaceholder")}
            value={input}
            onChange={(event) => setInput(event.target.value)}
          />
        </div>
        <StatusMenu value={status} onChange={setStatus} />
        <button
          type="button"
          className="chip review-chip"
          aria-pressed={review}
          onClick={() => setReview((value) => !value)}
        >
          {t("memory.filter.review", { count: reviewCount })}
        </button>
        {tree.data && <ScopeSelect tree={tree.data} scope={scope} onSelect={setScope} />}
      </div>
      <div className="memory-panes">
        {tree.data ? (
          <ScopePane tree={tree.data} scope={scope} onSelect={setScope} />
        ) : (
          <div className="memory-scopes">
            {tree.error ? (
              <LoadError error={tree.error} onRetry={tree.reload} />
            ) : (
              <p className="muted small" role="status">
                {t("app.loading")}
              </p>
            )}
          </div>
        )}
        <div className="memory-list-pane">
          {list.error ? (
            <LoadError error={list.error} onRetry={list.reload} />
          ) : list.loading && !list.data ? (
            <p className="muted small" role="status">
              {t("app.loading")}
            </p>
          ) : shown.length > 0 ? (
            <MemoryList items={shown} tree={tree.data} selected={selected} />
          ) : (
            <p className="memory-empty muted small">
              {items.length === 0 && !query ? t("memory.list.empty") : t("memory.list.noMatch")}
            </p>
          )}
        </div>
        <section className="memory-detail-pane" aria-label={t("memory.detail.label")}>
          {selected ? (
            <MemoryDetail
              key={selected}
              source={source}
              memoryId={selected}
              onChanged={onChanged}
            />
          ) : (
            <p className="memory-empty muted small">{t("memory.detail.none")}</p>
          )}
        </section>
      </div>
    </div>
  );
}

/** The メモリ screen, or its "not available yet" state while no source exists. */
export function MemoryPage() {
  const source = useMemorySource();
  if (!source) return <MemoryUnavailable />;
  return <MemoryScreen source={source} />;
}
